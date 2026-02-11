import os
from django.core.management.base import BaseCommand
from django.contrib.gis.gdal import DataSource
from django.conf import settings
from location.models import Country, State, District


class Command(BaseCommand):
    help = 'Load India states and districts from shapefile'

    def handle(self, *args, **options):
        shapefile_path = os.path.join(settings.BASE_DIR, '..', 'data', 'india', 'output.shp')

        if not os.path.exists(shapefile_path):
            self.stdout.write(self.style.ERROR(f'Shapefile not found: {shapefile_path}'))
            return

        self.stdout.write(self.style.SUCCESS(f'Loading data from {shapefile_path}'))

        ds = DataSource(shapefile_path)
        layer = ds[0]

        country, created = Country.objects.get_or_create(name="India", defaults={'code': 'IND'})
        if created:
            self.stdout.write(self.style.SUCCESS(f'Created country: {country.name}'))

        states_created = 0
        districts_created = 0
        errors = 0

        for feature in layer:
            state_name = feature.get("statename")
            district_name = feature.get("distname")

            if not state_name or not district_name:
                continue

            state, state_created = State.objects.get_or_create(
                name=state_name, country=country
            )
            if state_created:
                states_created += 1
                self.stdout.write(f'Created state: {state_name}')

            try:
                if District.objects.filter(name=district_name, state=state).exists():
                    continue

                District.objects.create(
                    poly=feature.geom.wkt,
                    state=state,
                    name=district_name
                )
                districts_created += 1
            except Exception:
                try:
                    District.objects.create(
                        multipoly=feature.geom.wkt,
                        is_multipoly=True,
                        state=state,
                        name=district_name
                    )
                    districts_created += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Error loading {district_name}: {e}'))
                    errors += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nData loading complete!\n'
            f'States created: {states_created}\n'
            f'Districts created: {districts_created}\n'
            f'Errors: {errors}'
        ))
