"""Conservative processing floor; shared by sizing and its menu preview."""
import math

MIN_NR_WIDTH = 256
MIN_NR_HEIGHT = 144


def safe_processing_size(source_w, source_h, width, height):
    """Raise tiny downscales while keeping the source aspect and source bounds."""
    mw, mh = min(MIN_NR_WIDTH, source_w), min(MIN_NR_HEIGHT, source_h)
    if width >= mw and height >= mh:
        return width, height
    scale = min(1., max(mw / source_w, mh / source_h,
                       width / source_w, height / source_h))
    return (min(source_w, math.ceil(source_w * scale / 2) * 2),
            min(source_h, math.ceil(source_h * scale / 2) * 2))
