"""Compose death answers and deterministic position diagrams without GUI dependencies."""

import base64
from html import escape
import io
import math


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def render_death_map(analysis, replay, names, actor_roles=None):
    """Return a focused SVG for the selected player, tanks, and primary boss."""
    position = analysis.get('position') or {}
    map_id = position.get('map_id')
    if map_id is None or not all(_finite(position.get(k)) for k in ('x', 'y')):
        return {'status': 'unavailable', 'reason': 'No usable position recorded at or before death.'}
    moment = analysis['time_seconds']
    victim = analysis['player']['id']
    actor_roles = dict(actor_roles or {})
    actor_roles[victim] = 'player'
    actors = {}
    for guid, samples in replay['actors'].items():
        if actor_roles and guid not in actor_roles:
            continue
        points = [p for p in samples if p['t'] <= moment and p['map_id'] == map_id
                  and all(_finite(p.get(k)) for k in ('x', 'y', 'sample_age_seconds'))
                  and p['sample_age_seconds'] <= 3]
        if points and points[-1]['sample_age_seconds'] + moment - points[-1]['t'] <= 3:
            actors[guid] = points
    # Replay grids need not land on the exact death time. Use its authoritative position.
    age = position.get('sample_age_seconds', 0)
    actors.setdefault(victim, []).append({
        'x': position['x'], 'y': position['y'], 't': moment,
        'sample_age_seconds': age})
    points = [p for samples in actors.values() for p in samples]
    low_x, high_x = min(p['x'] for p in points), max(p['x'] for p in points)
    low_y, high_y = min(p['y'] for p in points), max(p['y'] for p in points)
    span = max(high_x - low_x, high_y - low_y, 10) * 1.25
    center_x, center_y = (low_x + high_x) / 2, (low_y + high_y) / 2

    def project(p):
        return (400 + (p['x'] - center_x) * 440 / span,
                295 - (p['y'] - center_y) * 440 / span)

    title = f"{analysis['player']['name']} — death at {moment:.2f}s"
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" '
           'viewBox="0 0 800 600" role="img" aria-labelledby="title desc">',
           f'<title id="title">{escape(title)}</title>',
           '<desc id="desc">Recorded positions and movement for the selected player, tanks, '
           'and boss; schematic coordinates, no arena background.</desc>',
           '<rect width="800" height="600" fill="#101827"/>',
           '<g font-family="sans-serif" font-size="14" fill="#e6edf7">',
           f'<text x="24" y="30">{escape(title)}</text>',
           f'<text x="24" y="54">Map {escape(str(map_id))} · Recorded positions · X right / Y up</text>']
    for step in range(6):
        pixel = 75 + step * 88
        svg.append(f'<path d="M 180 {pixel} H 620 M {180 + step * 88} 75 V 515" '
                   'stroke="#263449" fill="none"/>')
    for guid, samples in actors.items():
        role = actor_roles.get(guid, 'player')
        color = {'player': '#ff7070', 'tank': '#72c9ff', 'boss': '#ffb454'}.get(
            role, '#72c9ff')
        path = ' '.join(f'{x:.2f},{y:.2f}' for x, y in map(project, samples))
        svg.append(f'<polyline points="{path}" fill="none" stroke="{color}" opacity="0.55"/>')
        p = samples[-1]
        x, y = project(p)
        role_label = {'player': 'selected player', 'tank': 'tank', 'boss': 'boss'}.get(role)
        label = names.get(guid, guid)
        if role_label:
            label += f' · {role_label}'
        sample_age = p['sample_age_seconds'] + moment - p['t']
        if sample_age:
            label += f' (sample {sample_age:.1f}s old)'
        if guid == victim:
            svg.append(f'<path d="M {x-7:.2f} {y-7:.2f} l 14 14 m 0 -14 l -14 14" '
                       f'stroke="{color}" stroke-width="3"/>')
        else:
            svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}"/>')
        svg.append(f'<text x="{x+10:.2f}" y="{y-8:.2f}" fill="{color}">{escape(label)}</text>')
    svg.extend(['<text x="24" y="555">Red: selected player · Blue: tanks · Orange: boss · Lines: movement</text>',
                '<text x="24" y="579">Samples older than 3s omitted; position evidence may be incomplete.</text>',
                '</g></svg>'])
    encoded = base64.b64encode(''.join(svg).encode('utf-8')).decode('ascii')
    return {'status': 'available', 'mime_type': 'image/svg+xml', 'encoding': 'base64',
            'data': encoded, 'width': 800, 'height': 600, 'alt_text': title,
            'map_id': map_id, 'time_seconds': moment, 'kind': 'position_diagram',
            'position_confidence': position.get('confidence'),
            'sample_age_seconds': age}


def render_death_animation(analysis, replay, names, actor_roles=None, seconds=5, fps=5):
    """Render a compact GIF ending at death from the focused replay actors."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {'status': 'unavailable', 'reason': 'GIF renderer is not installed.'}
    position = analysis.get('position') or {}
    map_id = position.get('map_id')
    if map_id is None or not all(_finite(position.get(k)) for k in ('x', 'y')):
        return {'status': 'unavailable', 'reason': 'No usable position recorded at death.'}
    moment = float(analysis['time_seconds'])
    start = max(float(replay.get('start') or 0), moment - max(1, min(float(seconds), 5)))
    fps = max(2, min(int(fps), 5))
    victim = str(analysis['player']['id'])
    roles = dict(actor_roles or {})
    roles[victim] = 'player'
    tracks = {}
    for guid, samples in replay.get('actors', {}).items():
        if guid not in roles:
            continue
        points = [p for p in samples if start <= float(p.get('t') or 0) <= moment
                  and p.get('map_id') == map_id
                  and all(_finite(p.get(k)) for k in ('x', 'y', 'sample_age_seconds'))]
        if points:
            tracks[guid] = points
    tracks.setdefault(victim, []).append({
        'x': position['x'], 'y': position['y'], 't': moment,
        'sample_age_seconds': position.get('sample_age_seconds', 0), 'map_id': map_id,
    })
    all_points = [point for samples in tracks.values() for point in samples]
    if not all_points:
        return {'status': 'unavailable', 'reason': 'No focused position samples are available.'}
    low_x, high_x = min(p['x'] for p in all_points), max(p['x'] for p in all_points)
    low_y, high_y = min(p['y'] for p in all_points), max(p['y'] for p in all_points)
    span = max(high_x - low_x, high_y - low_y, 10) * 1.25
    center_x, center_y = (low_x + high_x) / 2, (low_y + high_y) / 2

    def project(point):
        return (320 + (point['x'] - center_x) * 340 / span,
                235 - (point['y'] - center_y) * 340 / span)

    colors = {'player': '#ff7070', 'tank': '#72c9ff', 'boss': '#ffb454'}
    font = ImageFont.load_default()
    frame_count = max(2, int(round((moment - start) * fps)) + 1)
    frames = []
    for index in range(frame_count):
        frame_time = moment if index == frame_count - 1 else start + index / fps
        frame = Image.new('RGB', (640, 480), '#101827')
        draw = ImageDraw.Draw(frame)
        for step in range(6):
            pixel = 55 + step * 68
            draw.line((150, pixel, 490, pixel), fill='#263449')
            draw.line((150 + step * 68, 55, 150 + step * 68, 395), fill='#263449')
        draw.text((16, 14), f"{analysis['player']['name']} - {frame_time:.1f}s / death {moment:.1f}s",
                  fill='#e6edf7', font=font)
        for guid, samples in tracks.items():
            visible = [p for p in samples if float(p['t']) <= frame_time
                       and float(p.get('sample_age_seconds') or 0) + frame_time - float(p['t']) <= 3]
            if not visible:
                continue
            recent = [p for p in visible if float(p['t']) >= frame_time - 1.0]
            if len(recent) > 1:
                draw.line([project(p) for p in recent], fill=colors[roles[guid]], width=2)
            x, y = project(visible[-1])
            color = colors[roles[guid]]
            if guid == victim and index == frame_count - 1:
                draw.line((x - 7, y - 7, x + 7, y + 7), fill=color, width=3)
                draw.line((x + 7, y - 7, x - 7, y + 7), fill=color, width=3)
            else:
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)
            draw.text((x + 8, y - 10), str(names.get(guid, guid))[:28], fill=color, font=font)
        draw.text((16, 450), 'Red: player  Blue: tanks  Orange: boss',
                  fill='#e6edf7', font=font)
        frames.append(frame)
    output = io.BytesIO()
    durations = [round(1000 / fps)] * len(frames)
    durations[-1] = 800
    try:
        frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, disposal=2, optimize=False)
    except Exception:
        return {'status': 'unavailable', 'reason': 'GIF encoding failed.'}
    data = output.getvalue()
    if len(data) > 8 * 1024 * 1024:
        return {'status': 'unavailable', 'reason': 'GIF exceeded the attachment size limit.'}
    title = f"{analysis['player']['name']} — five seconds before death"
    return {'status': 'available', 'mime_type': 'image/gif', 'encoding': 'base64',
            'data': base64.b64encode(data).decode('ascii'), 'width': 640, 'height': 480,
            'frames': len(frames), 'fps': fps, 'duration_seconds': moment - start,
            'alt_text': title, 'map_id': map_id, 'time_seconds': moment,
            'kind': 'position_animation'}


def build_death_answer(analysis, replay, names, actor_roles=None):
    """Keep narrative, evidence, replay and visual in one answer-generation payload."""
    player = analysis['player']['name']
    text = f"{player} died at {analysis['time_seconds']:.2f}s."
    killing = analysis.get('killing_blow') or {}
    primary = analysis.get('primary_cause') or {}
    if killing.get('ability'):
        provenance = killing.get('provenance', 'unknown')
        text += f" Killing blow: {killing['ability']} ({provenance})."
    if primary.get('ability'):
        text += f" Likely primary cause: {primary['ability']} (inferred from recent damage)."
    image = render_death_map(analysis, replay, names, actor_roles)
    animation = render_death_animation(analysis, replay, names, actor_roles)
    return {'death_id': analysis['death_id'], 'answer': {'text': text},
            'analysis': analysis, 'replay': replay, 'image': image, 'animation': animation}
