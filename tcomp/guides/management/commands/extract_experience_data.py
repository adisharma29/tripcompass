"""
Extract moods and experiences data from preview.html JavaScript objects.

Parses the JS `moods` and `experiences` objects from the HTML file and
writes them as clean JSON files for use by the import_shimla_data command.
"""
import json
import os
import re

from django.core.management.base import BaseCommand


def js_obj_to_json(js_text):
    """Convert a JS object literal to valid JSON."""
    text = js_text

    # Replace single quotes with double quotes (but not inside strings)
    # First, handle escaped single quotes inside strings
    text = re.sub(r"(?<![\\])'", '"', text)

    # Fix JS keys without quotes: `key:` -> `"key":`
    text = re.sub(r'(?m)^\s*(\w+)\s*:', r'"\1":', text)
    text = re.sub(r',\s*(\w+)\s*:', r', "\1":', text)
    text = re.sub(r'\{\s*(\w+)\s*:', r'{ "\1":', text)

    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)

    # Fix booleans
    text = text.replace(': true', ': true').replace(': false', ': false')

    # Handle \u2019 etc — these are already valid JSON escapes
    # Handle \n in strings that are literal newlines in JS template
    # Actually JS \n inside quotes is fine in JSON

    return text


class Command(BaseCommand):
    help = 'Extract moods and experiences from preview.html into JSON files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            default=None,
            help='Path to preview.html (defaults to $TCOMP_SRC/preview.html or /tcomp-src/preview.html)'
        )
        parser.add_argument(
            '--output-dir',
            default=None,
            help='Output directory for JSON files (defaults to data/ in project root)'
        )

    def handle(self, *args, **options):
        tcomp_src = os.environ.get('TCOMP_SRC', '/tcomp-src')
        source_path = options['source'] or os.path.join(tcomp_src, 'preview.html')
        from django.conf import settings
        output_dir = options['output_dir'] or os.path.join(settings.BASE_DIR, '..', 'data')

        os.makedirs(output_dir, exist_ok=True)

        with open(source_path, 'r') as f:
            content = f.read()

        # Extract moods
        self.stdout.write('Extracting moods...')
        moods = self._extract_moods(content)
        moods_path = os.path.join(output_dir, 'moods.json')
        with open(moods_path, 'w') as f:
            json.dump(moods, f, indent=2, ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(f'Wrote {len(moods)} moods to {moods_path}'))

        # Extract experiences
        self.stdout.write('Extracting experiences...')
        experiences = self._extract_experiences(content)
        exp_path = os.path.join(output_dir, 'experiences_content.json')
        with open(exp_path, 'w') as f:
            json.dump(experiences, f, indent=2, ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(experiences)} experiences to {exp_path}'
        ))

    def _extract_moods(self, content):
        """Parse the moods JS object into a list of mood dicts."""
        moods_match = re.search(r'const\s+moods\s*=\s*(\{.*?\n\s*\});', content, re.DOTALL)
        if not moods_match:
            self.stdout.write(self.style.ERROR('Could not find moods object'))
            return []

        js_text = moods_match.group(1)
        moods = []

        # Parse each mood block manually for reliability
        mood_pattern = re.compile(
            r"'([^']+)':\s*\{(.*?)\}(?=,\s*'|\s*\}$)",
            re.DOTALL
        )

        for match in mood_pattern.finditer(js_text):
            mood_id = match.group(1)
            block = match.group(2)

            mood = {'id': mood_id}
            mood['name'] = self._extract_field(block, 'name')
            mood['tagline'] = self._extract_field(block, 'tagline')
            mood['tip'] = self._extract_field(block, 'tip')
            mood['illustration'] = self._extract_field(block, 'illustration')
            mood['color'] = self._extract_field(block, 'color')

            support_line = self._extract_field(block, 'supportLine')
            if support_line:
                mood['supportLine'] = support_line

            is_special = re.search(r'isSpecial:\s*true', block)
            mood['isSpecial'] = bool(is_special)

            # Extract experiences list
            exp_match = re.search(r'experiences:\s*\[(.*?)\]', block, re.DOTALL)
            if exp_match:
                exp_names = re.findall(r"'([^']+)'", exp_match.group(1))
                mood['experiences'] = exp_names
            else:
                mood['experiences'] = []

            moods.append(mood)

        return moods

    def _extract_experiences(self, content):
        """Parse the experiences JS object into a list of experience dicts."""
        idx = content.find('const experiences')
        if idx == -1:
            self.stdout.write(self.style.ERROR('Could not find experiences object'))
            return []

        exp_content = content[idx:]

        # Find all experience names
        names = re.findall(r"'([^']+)':\s*\{\s*name:\s*'", exp_content[:80000])

        experiences = []
        for i, name in enumerate(names):
            # Find start of this experience
            start = exp_content.find(f"'{name}':", 0 if i == 0 else 0)
            if start == -1:
                continue

            # Find end (next experience or end of object)
            if i + 1 < len(names):
                end = exp_content.find(f"'{names[i+1]}':", start + len(name))
            else:
                end = exp_content.find('};', start)

            block = exp_content[start:end]

            exp = {'name': name}
            exp['mood'] = self._extract_field(block, 'mood')
            exp['color'] = self._extract_field(block, 'color')
            exp['tagline'] = self._extract_field(block, 'tagline')
            exp['type'] = self._extract_field(block, 'type')
            exp['duration'] = self._extract_field(block, 'duration')
            exp['effort'] = self._extract_field(block, 'effort')
            exp['whyWeChoseThis'] = self._extract_field(block, 'whyWeChoseThis')
            exp['goldenWay'] = self._extract_field(block, 'goldenWay')

            # Extract images array
            images_match = re.search(r'images:\s*\[(.*?)\]', block, re.DOTALL)
            if images_match:
                exp['images'] = re.findall(r'"([^"]+)"', images_match.group(1))
            else:
                exp['images'] = []

            # Extract about array
            exp['about'] = self._extract_string_array(block, 'about')
            exp['whatYouGet'] = self._extract_string_array(block, 'whatYouGet')

            # Extract breakdown as nested dict with arrays
            exp['breakdown'] = self._extract_breakdown(block)

            # Extract center and zoom
            center_match = re.search(r'center:\s*\[([^]]+)\]', block)
            if center_match:
                coords = center_match.group(1).split(',')
                try:
                    exp['center'] = [float(coords[0].strip()), float(coords[1].strip())]
                except (ValueError, IndexError):
                    exp['center'] = [0, 0]
            else:
                exp['center'] = [0, 0]

            zoom_match = re.search(r'zoom:\s*([0-9.]+)', block)
            exp['zoom'] = float(zoom_match.group(1)) if zoom_match else 14

            # Related experiences
            related_match = re.search(r'relatedExperiences:\s*\[(.*?)\]', block, re.DOTALL)
            if related_match:
                exp['relatedExperiences'] = re.findall(r"'([^']+)'", related_match.group(1))
            else:
                exp['relatedExperiences'] = []

            experiences.append(exp)

        return experiences

    def _extract_field(self, block, field_name):
        """Extract a single-quoted string field value."""
        # Try single-quoted value first
        match = re.search(
            rf"{field_name}:\s*'((?:[^'\\]|\\.)*)'",
            block,
            re.DOTALL
        )
        if match:
            return match.group(1).replace("\\'", "'").replace("\\n", "\n")

        # Try double-quoted
        match = re.search(
            rf'{field_name}:\s*"((?:[^"\\]|\\.)*)"',
            block,
            re.DOTALL
        )
        if match:
            return match.group(1).replace('\\"', '"').replace("\\n", "\n")

        return ''

    def _extract_string_array(self, block, field_name):
        """Extract an array of single-quoted strings."""
        # Find the array block
        pattern = rf'{field_name}:\s*\[(.*?)\]'
        match = re.search(pattern, block, re.DOTALL)
        if not match:
            return []
        arr_text = match.group(1)
        items = re.findall(r"'((?:[^'\\]|\\.)*)'", arr_text, re.DOTALL)
        return [item.replace("\\'", "'").replace("\\n", "\n") for item in items]

    def _extract_breakdown(self, block):
        """Extract the breakdown nested object."""
        bd_start = block.find('breakdown:')
        if bd_start == -1:
            return {}

        # Find the opening brace
        brace_start = block.find('{', bd_start)
        if brace_start == -1:
            return {}

        # Find matching closing brace (handle nesting)
        depth = 0
        pos = brace_start
        while pos < len(block):
            if block[pos] == '{':
                depth += 1
            elif block[pos] == '}':
                depth -= 1
                if depth == 0:
                    break
            pos += 1

        bd_text = block[brace_start:pos + 1]

        # Parse each key in the breakdown
        breakdown = {}
        # Find keys that have string values
        for match in re.finditer(r'(\w+):\s*\'((?:[^\'\\]|\\.)*)\'', bd_text):
            key = match.group(1)
            value = match.group(2).replace("\\'", "'").replace("\\n", "\n")
            breakdown[key] = value

        # Find keys that have array values
        for match in re.finditer(r'(\w+):\s*\[(.*?)\]', bd_text, re.DOTALL):
            key = match.group(1)
            arr_text = match.group(2)
            items = re.findall(r"'((?:[^'\\]|\\.)*)'", arr_text, re.DOTALL)
            if items:
                breakdown[key] = [
                    item.replace("\\'", "'").replace("\\n", "\n")
                    for item in items
                ]

        return breakdown
