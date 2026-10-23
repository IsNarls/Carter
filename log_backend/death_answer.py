"""Compose death answers and deterministic position diagrams without GUI dependencies."""

import base64
from html import escape
import math


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def render_death_map(analysis, replay, names):
    """Return an inline SVG on the victim's floor using only pre-death samples."""
    position = analysis.get('position') or {}
    map_id = position.get('map_id')
    if map_id is None or not all(_finite(position.get(k)) for k in ('x', 'y')):
        return {'status': 'unavailable', 'reason': 'No usable position recorded at or before death.'}
    moment = analysis['time_seconds']
    victim = analysis['player']['id']
    actors = {}
    for guid, samples in replay['actors'].items():
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
           '<desc id="desc">Recorded positions and movement before death; '
           'schematic coordinates, no arena background. Red cross marks the victim.</desc>',
           '<rect width="800" height="600" fill="#101827"/>',
           '<g font-family="sans-serif" font-size="14" fill="#e6edf7">',
           f'<text x="24" y="30">{escape(title)}</text>',
           f'<text x="24" y="54">Map {escape(str(map_id))} · Recorded positions · X right / Y up</text>']
    for step in range(6):
        pixel = 75 + step * 88
        svg.append(f'<path d="M 180 {pixel} H 620 M {180 + step * 88} 75 V 515" '
                   'stroke="#263449" fill="none"/>')
    for guid, samples in actors.items():
        color = '#ff7070' if guid == victim else '#72c9ff'
        path = ' '.join(f'{x:.2f},{y:.2f}' for x, y in map(project, samples))
        svg.append(f'<polyline points="{path}" fill="none" stroke="{color}" opacity="0.55"/>')
        p = samples[-1]
        x, y = project(p)
        label = names.get(guid, guid)
        sample_age = p['sample_age_seconds'] + moment - p['t']
        if sample_age:
            label += f' (sample {sample_age:.1f}s old)'
        if guid == victim:
            svg.append(f'<path d="M {x-7:.2f} {y-7:.2f} l 14 14 m 0 -14 l -14 14" '
                       f'stroke="{color}" stroke-width="3"/>')
        else:
            svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}"/>')
        svg.append(f'<text x="{x+10:.2f}" y="{y-8:.2f}" fill="{color}">{escape(label)}</text>')
    svg.extend(['<text x="24" y="555">Red cross: death position · Blue: other actors · Lines: recorded movement</text>',
                '<text x="24" y="579">Other actor samples older than 3s omitted; position evidence may be incomplete.</text>',
                '</g></svg>'])
    encoded = base64.b64encode(''.join(svg).encode('utf-8')).decode('ascii')
    return {'status': 'available', 'mime_type': 'image/svg+xml', 'encoding': 'base64',
            'data': encoded, 'width': 800, 'height': 600, 'alt_text': title,
            'map_id': map_id, 'time_seconds': moment, 'kind': 'position_diagram',
            'position_confidence': position.get('confidence'),
            'sample_age_seconds': age}


def build_death_answer(analysis, replay, names):
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
    image = render_death_map(analysis, replay, names)
    return {'death_id': analysis['death_id'], 'answer': {'text': text},
            'analysis': analysis, 'replay': replay, 'image': image}
