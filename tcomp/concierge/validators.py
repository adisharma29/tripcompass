import io

from django.core.exceptions import ValidationError
from PIL import Image

ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_DIMENSION = 2048


# Magic bytes for allowed image types
MAGIC_BYTES = {
    b'\xff\xd8\xff': 'image/jpeg',
    b'\x89PNG': 'image/png',
    b'RIFF': 'image/webp',  # WebP starts with RIFF....WEBP
}


def _detect_content_type(file):
    """Detect image type from magic bytes, not Content-Type header."""
    pos = file.tell()
    header = file.read(12)
    file.seek(pos)

    for magic, content_type in MAGIC_BYTES.items():
        if header.startswith(magic):
            if content_type == 'image/webp' and b'WEBP' not in header:
                continue
            return content_type
    return None


def validate_image_upload(file):
    """Validate and sanitize an uploaded image file.

    1. Check magic bytes (don't trust Content-Type header)
    2. Reject SVG, GIF, BMP, TIFF
    3. Check file size <= 5MB
    4. Re-save with Pillow to strip EXIF
    5. Resize to max 2048px longest edge
    """
    # Size check
    if file.size > MAX_IMAGE_SIZE:
        raise ValidationError(
            f'Image file too large. Maximum size is {MAX_IMAGE_SIZE // (1024 * 1024)} MB.'
        )

    # Magic bytes check
    content_type = _detect_content_type(file)
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValidationError(
            'Unsupported image type. Allowed: JPEG, PNG, WebP.'
        )

    # Re-save with Pillow to strip EXIF and resize
    try:
        file.seek(0)
        img = Image.open(file)
        img.verify()  # Verify it's a valid image
        file.seek(0)
        img = Image.open(file)  # Re-open after verify
    except Exception:
        raise ValidationError('Invalid or corrupted image file.')

    # Resize if needed
    if max(img.size) > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    # Convert to RGB if needed (for JPEG output)
    if img.mode in ('RGBA', 'P'):
        output_format = 'PNG'
    else:
        img = img.convert('RGB')
        output_format = 'JPEG'

    # Re-encode to strip EXIF and any embedded scripts
    buffer = io.BytesIO()
    img.save(buffer, format=output_format, quality=85)
    buffer.seek(0)

    return buffer, output_format.lower()
