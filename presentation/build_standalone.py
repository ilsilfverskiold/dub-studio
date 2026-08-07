#!/usr/bin/env python3
"""Build presentation_standalone.html — one self-contained file with all videos
embedded inside it. Double-click the result in any browser; no server, no folders.
Rerun this after editing presentation.html:  python3 build_standalone.py"""
import base64, pathlib, re

here = pathlib.Path(__file__).parent
src = (here / "presentation.html").read_text()

def inline(m):
    path = here / m.group(1)
    data = base64.b64encode(path.read_bytes()).decode()
    print(f"  embedded {m.group(1)}  ({path.stat().st_size/1e6:.1f} MB)")
    return f'src="data:video/mp4;base64,{data}"'

out = re.sub(r'src="(demo_videos/[^"]+\.mp4)"', inline, src)
dest = here / "presentation_standalone.html"
dest.write_text(out)
print(f"wrote {dest.name}  ({dest.stat().st_size/1e6:.1f} MB)")
