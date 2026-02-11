"""
Import Shimla travel guide data into the database.

Reads from JSON data files and creates Destination, Moods, Experiences,
ExperienceImages, GeoFeatures, and NearbyPlaces. Idempotent via get_or_create.

Prerequisites:
  - Run extract_experience_data first to generate moods.json + experiences_content.json
  - Ensure data/spreadsheet_data.json, data/shimla-routes.geojson,
    data/nearby_places_data.json exist
"""
import json
import os
import shutil

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Point
from django.core.files import File
from django.core.management.base import BaseCommand

from guides.models import (
    Destination, Mood, Experience, ExperienceImage,
    GeoFeature, NearbyPlace,
)
from location.models import Country, State

# Tcomp frontend source directory (for copying images)
# In Docker: mounted at /tcomp-src; locally: set via --tcomp-src or TCOMP_SRC env
TCOMP_SRC = os.environ.get('TCOMP_SRC', '/tcomp-src')


class Command(BaseCommand):
    help = 'Import Shimla travel guide data from JSON files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--data-dir',
            default=None,
            help='Path to data directory (defaults to /data if in Docker, else project_root/data/)'
        )
        parser.add_argument(
            '--tcomp-src',
            default=None,
            help='Path to tcomp frontend source (for copying images)'
        )

    def handle(self, *args, **options):
        # In Docker, data is mounted at /data; locally it's at project_root/data/
        default_data_dir = '/data' if os.path.isdir('/data') else os.path.join(settings.BASE_DIR, '..', 'data')
        data_dir = options['data_dir'] or default_data_dir

        global TCOMP_SRC
        if options['tcomp_src']:
            TCOMP_SRC = options['tcomp_src']

        self.stdout.write('Starting Shimla data import...\n')

        # 1. Location hierarchy
        country, _ = Country.objects.get_or_create(name='India', defaults={'code': 'IND'})
        state, _ = State.objects.get_or_create(name='Himachal Pradesh', country=country)
        self.stdout.write(self.style.SUCCESS('Location hierarchy ready'))

        # 2. Destination
        destination, created = Destination.objects.get_or_create(
            slug='shimla',
            defaults={
                'name': 'Shimla',
                'tagline': 'The Queen of Hills',
                'state': state,
                'center_lng': 77.1734,
                'center_lat': 31.1048,
                'default_zoom': 13.5,
                'default_pitch': 45,
                'default_bearing': -17.6,
                'background_color': '#FFE9CF',
                'text_color': '#434431',
                'is_published': True,
            }
        )
        action = 'Created' if created else 'Found existing'
        self.stdout.write(self.style.SUCCESS(f'{action} destination: Shimla'))

        # 3. Moods
        moods_path = os.path.join(data_dir, 'moods.json')
        if not os.path.exists(moods_path):
            self.stdout.write(self.style.ERROR(
                f'moods.json not found at {moods_path}. Run extract_experience_data first.'
            ))
            return

        with open(moods_path) as f:
            moods_data = json.load(f)

        mood_map = {}  # slug -> Mood instance
        for i, md in enumerate(moods_data):
            mood, created = Mood.objects.get_or_create(
                destination=destination,
                slug=md['id'],
                defaults={
                    'name': md['name'],
                    'tagline': md.get('tagline', ''),
                    'tip': md.get('tip', ''),
                    'support_line': md.get('supportLine', ''),
                    'color': md.get('color', '#000000'),
                    'is_special': md.get('isSpecial', False),
                    'sort_order': i,
                }
            )
            mood_map[md['id']] = mood

            # Copy illustration if available
            if created and md.get('illustration'):
                img_src = os.path.join(TCOMP_SRC, md['illustration'])
                if os.path.exists(img_src):
                    with open(img_src, 'rb') as img_file:
                        filename = os.path.basename(img_src)
                        mood.illustration.save(filename, File(img_file), save=True)

            self.stdout.write(f'  {"Created" if created else "Exists"}: {mood.name}')

        self.stdout.write(self.style.SUCCESS(f'Moods: {len(mood_map)} ready\n'))

        # 4. Experiences
        experiences_path = os.path.join(data_dir, 'experiences_content.json')
        spreadsheet_path = os.path.join(data_dir, 'spreadsheet_data.json')

        if not os.path.exists(experiences_path):
            self.stdout.write(self.style.ERROR(
                f'experiences_content.json not found. Run extract_experience_data first.'
            ))
            return

        with open(experiences_path) as f:
            experiences_data = json.load(f)
        with open(spreadsheet_path) as f:
            spreadsheet_data = json.load(f)

        # Build name -> spreadsheet row mapping
        spreadsheet_map = {}
        for row in spreadsheet_data:
            spreadsheet_map[row['title']] = row

        # Build name -> experience content mapping
        exp_content_map = {e['name']: e for e in experiences_data}

        exp_map = {}  # name -> Experience instance
        for row in spreadsheet_data:
            code = row['id']
            name = row['title']
            content = exp_content_map.get(name, {})

            mood_slug = content.get('mood', '')
            mood = mood_map.get(mood_slug)

            effort_raw = content.get('effort', row.get('effort_level', '')).lower()
            effort = effort_raw if effort_raw in ['easy', 'gentle', 'fair', 'moderate', 'challenging'] else ''

            center = content.get('center', [0, 0])

            experience, created = Experience.objects.get_or_create(
                code=code,
                defaults={
                    'destination': destination,
                    'mood': mood,
                    'name': name,
                    'tagline': content.get('tagline', row.get('short_brief', '')[:500]),
                    'experience_type': content.get('type', row.get('experience_type', '')),
                    'color': content.get('color', mood.color if mood else '#000000'),
                    'duration': content.get('duration', row.get('min_enjoyment_time', '')),
                    'effort': effort,
                    'distance': row.get('distance', ''),
                    'best_time': row.get('best_time', ''),
                    'about': content.get('about', []),
                    'what_you_get': content.get('whatYouGet', []),
                    'why_we_chose_this': content.get('whyWeChoseThis', row.get('why_chosen', '')),
                    'golden_way': content.get('goldenWay', row.get('golden_way', '')),
                    'breakdown': content.get('breakdown', {}),
                    'spreadsheet_data': row,
                    'center_lng': center[0] if len(center) > 0 else 0,
                    'center_lat': center[1] if len(center) > 1 else 0,
                    'zoom': content.get('zoom', 14),
                    'sort_order': int(code.replace('SML', '').lstrip('0') or '0'),
                    'is_published': True,
                }
            )
            exp_map[name] = experience
            self.stdout.write(f'  {"Created" if created else "Exists"}: {code} - {name}')

        self.stdout.write(self.style.SUCCESS(f'Experiences: {len(exp_map)} ready\n'))

        # 5. Experience Images
        images_path = os.path.join(data_dir, 'experience_images.json')
        if os.path.exists(images_path):
            with open(images_path) as f:
                images_data = json.load(f)

            img_count = 0
            for code, image_paths in images_data.items():
                experience = Experience.objects.filter(code=code).first()
                if not experience:
                    continue

                for i, img_rel_path in enumerate(image_paths):
                    # Image path is like "images/experiences/SML001/filename.JPG"
                    # Actual file is at carousel_images/SML001_filename.JPG
                    filename = os.path.basename(img_rel_path)
                    carousel_filename = f"{code}_{filename}"
                    img_src = os.path.join(TCOMP_SRC, 'carousel_images', carousel_filename)

                    if not os.path.exists(img_src):
                        continue

                    existing = ExperienceImage.objects.filter(
                        experience=experience,
                        alt_text=filename
                    ).exists()
                    if existing:
                        continue

                    img = ExperienceImage(
                        experience=experience,
                        alt_text=filename,
                        sort_order=i,
                    )
                    with open(img_src, 'rb') as img_file:
                        img.image.save(filename, File(img_file), save=True)
                    img_count += 1

            self.stdout.write(self.style.SUCCESS(f'Experience images: {img_count} imported\n'))

        # 6. GeoFeatures
        geojson_path = os.path.join(data_dir, 'shimla-routes.geojson')
        if os.path.exists(geojson_path):
            with open(geojson_path) as f:
                geojson_data = json.load(f)

            geo_count = 0
            for feature in geojson_data.get('features', []):
                props = feature.get('properties', {})
                geom = feature.get('geometry', {})
                geom_type = geom.get('type', '')

                name = props.get('name', 'Unnamed')

                # Determine feature type
                if geom_type == 'LineString':
                    feature_type = 'route'
                elif geom_type == 'Polygon':
                    feature_type = 'zone'
                elif geom_type == 'Point':
                    feature_type = 'poi'
                else:
                    continue

                # Try to match experience by name
                experience = exp_map.get(name)

                geos_geom = GEOSGeometry(json.dumps(geom), srid=4326)

                defaults = {
                    'destination': destination,
                    'experience': experience,
                    'description': props.get('description', ''),
                    'feature_type': feature_type,
                    'color': props.get('color', ''),
                    'fill_opacity': props.get('fillOpacity'),
                    'category': props.get('category', ''),
                    'poi_type': props.get('poiType', ''),
                    'folder': props.get('folder', ''),
                    'folder_category': props.get('folderCategory', ''),
                    'kml_source': props.get('kmlSource', ''),
                }

                if feature_type == 'route':
                    defaults['route'] = geos_geom
                elif feature_type == 'zone':
                    defaults['zone'] = geos_geom
                elif feature_type == 'poi':
                    defaults['point'] = geos_geom

                _, created = GeoFeature.objects.get_or_create(
                    name=name,
                    feature_type=feature_type,
                    destination=destination,
                    defaults=defaults,
                )
                if created:
                    geo_count += 1

            self.stdout.write(self.style.SUCCESS(f'GeoFeatures: {geo_count} imported\n'))

        # 7. Nearby Places
        nearby_path = os.path.join(data_dir, 'nearby_places_data.json')
        if os.path.exists(nearby_path):
            with open(nearby_path) as f:
                nearby_data = json.load(f)

            nearby_count = 0
            for exp_name, places in nearby_data.items():
                experience = exp_map.get(exp_name)
                if not experience:
                    self.stdout.write(
                        self.style.WARNING(f'  No experience found for: {exp_name}')
                    )
                    continue

                for place in places:
                    _, created = NearbyPlace.objects.get_or_create(
                        experience=experience,
                        google_place_id=place['id'],
                        defaults={
                            'destination': destination,
                            'name': place['name'],
                            'place_type': place.get('type', ''),
                            'primary_type': place.get('primaryType', ''),
                            'rating': place.get('rating'),
                            'user_rating_count': place.get('userRatingCount'),
                            'address': place.get('address', ''),
                            'location': Point(
                                float(place['lng']),
                                float(place['lat']),
                                srid=4326
                            ),
                        }
                    )
                    if created:
                        nearby_count += 1

            self.stdout.write(self.style.SUCCESS(f'Nearby places: {nearby_count} imported\n'))

        # 8. Set related experiences
        for content in experiences_data:
            name = content['name']
            related_names = content.get('relatedExperiences', [])
            if not related_names:
                continue
            experience = exp_map.get(name)
            if not experience:
                continue
            for rel_name in related_names:
                rel_exp = exp_map.get(rel_name)
                if rel_exp:
                    experience.related_experiences.add(rel_exp)

        self.stdout.write(self.style.SUCCESS('\nShimla data import complete!'))
