"""Pick the chat emoji subset the picker shows, and write the browser catalog.

`catalog.json` records the assets this repository keeps. The picker only needs a
fraction of what the download script fetches: 3,789 mini emojis and 4,652 stickers
were more than anyone scrolls through, so the browser catalog carries about a
tenth of them. Selection is deterministic, so re-running the download script
publishes the same list.

The animated WebP files average 413 KiB each -- 94 MiB for 233 stickers, too much
to push to the Render deployment -- so they are not stored here at all. The picker
loads them straight from Google's own CDN (the `source` URL the download script
already records). The color SVGs stay local; all of them together are 0.6 MiB.
"""
import json
import hashlib
import math
from pathlib import Path

# Roughly a tenth of the full catalog (3,789 mini emojis).
TARGET_ENTRIES = 379
# Faces carry most of the feeling in a chat, so they keep both the animated and
# the color version. Everything else shows one image, animated where it exists.
BOTH_PACK_CATEGORY = '표정'
# Packs served from their original CDN instead of this repository. Their files are
# never kept locally, so `--prune` deletes the folder and catalog.json records the
# remote URL only. fonts.gstatic.com sends `Access-Control-Allow-Origin: *`.
REMOTE_PACKS = ('noto',)
BUILTIN_PACKS = ('noto', 'openmoji')
# One manifest per custom sticker pack, e.g. custom-manifest.json (새담걸) and
# custom-manifest-office.json (사무용품). Read in name order so the catalog is stable.
CUSTOM_MANIFESTS = 'custom-manifest*.json'


def load_manifests(root):
    """Every custom sticker pack this repository ships, in a fixed order."""
    return [json.loads(path.read_text(encoding='utf-8'))
            for path in sorted(Path(root).glob(CUSTOM_MANIFESTS))]


def custom_packs(root):
    """The pack names the custom manifests define, alongside BUILTIN_PACKS."""
    return tuple(manifest['pack'] for manifest in load_manifests(root))


def load_custom_entries(root):
    """Build local sticker entries from the small, reviewed custom manifests."""
    entries = []
    for manifest in load_manifests(root):
        pack = manifest['pack']
        category = manifest['name']
        fallback = manifest.get('fallback', '🙂')
        for sticker in manifest['stickers']:
            relative = Path(manifest['folder'], sticker['file'])
            path = Path(root, relative)
            data = path.read_bytes()
            entries.append({
                'id': f"{pack}-{sticker['id']}",
                # A pack may name a closer stand-in per sticker; the image is
                # what shows, this only stands in when it fails to load.
                'emoji': sticker.get('emoji', fallback),
                'label': sticker['label'],
                'category': category,
                'mini': False,
                pack: {
                    'src': '/static/chat-emoji/' + relative.as_posix(),
                    'sha256': hashlib.sha256(data).hexdigest(),
                },
            })
    return entries


def merge_custom_entries(entries, root):
    """Replace generated custom records while leaving the standard catalog intact."""
    custom = load_custom_entries(root)
    custom_ids = {entry['id'] for entry in custom}
    pack_prefixes = {entry['id'].split('-', 1)[0] + '-' for entry in custom}
    standard = [entry for entry in entries if entry.get('id') not in custom_ids
                and not any(str(entry.get('id', '')).startswith(prefix) for prefix in pack_prefixes)]
    return custom + standard


def pack_url(record, pack):
    """Where the browser loads this image from: the CDN for remote packs, else here."""
    return record['source'] if pack in REMOTE_PACKS else record['src']


def select_entries(entries, target=TARGET_ENTRIES):
    """Return the subset shown in the picker, in the original catalog order."""
    # Custom character stickers are sticker-only and are always kept in addition
    # to the normal mini-emoji budget.
    pinned = [entry for entry in entries if entry.get('mini') is False]
    entries = [entry for entry in entries if entry.get('mini') is not False]
    # Running this on an already-selected catalog must not shrink it again: the
    # per-category quotas below round down, so a second pass would drop entries.
    if len(entries) <= target:
        return pinned + list(entries)
    # Entries without CLDR keywords are the hand-picked list at the top of
    # prepare_chat_emojis.py: short Korean names, the ones worth keeping whole.
    curated = [entry for entry in entries if 'keywords' not in entry]
    rest = [entry for entry in entries if 'keywords' in entry]
    budget = max(0, target - len(curated))

    by_category = {}
    for entry in rest:
        by_category.setdefault(entry['category'], []).append(entry)
    # Weighting by the square root keeps small categories visible instead of
    # letting '사람·인사' (2,237 skin-tone and gender variants) fill the panel.
    weights = {name: math.sqrt(len(items)) for name, items in by_category.items()}
    total_weight = sum(weights.values()) or 1

    keep = {entry['id'] for entry in curated}
    for name, items in by_category.items():
        quota = min(len(items), max(1, round(weights[name] / total_weight * budget)))
        # Prefer entries that actually have an image, then spread the picks
        # evenly over the category so the selection is not all one subgroup.
        pool = [entry for entry in items if 'noto' in entry or 'openmoji' in entry] or items
        step = len(pool) / quota
        for index in range(quota):
            keep.add(pool[int(index * step)]['id'])

    selected = [entry for entry in entries if entry['id'] in keep]
    return pinned + selected[:target]


def browser_entry(entry, packs):
    """Strip the bookkeeping fields and the second image where we only show one."""
    trimmed = {key: value for key, value in entry.items() if key != 'openmoji_code'}
    if entry['category'] != BOTH_PACK_CATEGORY and 'noto' in trimmed and 'openmoji' in trimmed:
        del trimmed['openmoji']
    for pack in packs:
        if pack in trimmed:
            trimmed[pack] = {'src': pack_url(trimmed[pack], pack)}
    return trimmed


def write_catalog_js(entries, root):
    """Write static/chat-emoji/catalog.js and report the two picker counts."""
    packs = (*BUILTIN_PACKS, *custom_packs(root))
    selected = [browser_entry(entry, packs) for entry in select_entries(entries)]
    payload = json.dumps(selected, ensure_ascii=False, separators=(',', ':'))
    Path(root, 'catalog.js').write_text('window.SAEDAM_CHAT_EMOJIS = ' + payload + ';\n', encoding='utf-8')
    return {
        'mini': sum(entry.get('mini') is not False for entry in selected),
        'stickers': sum(sum(pack in entry for pack in packs) for entry in selected),
    }


def prune_unused(entries, root):
    """Delete image files this repository does not serve, and trim catalog.json to match.

    That is every file the picker no longer shows, plus every REMOTE_PACKS file:
    those load from their original CDN, so keeping a copy here only bloats the
    deployment. Only run this on purpose (`--prune`); the files come back with
    `python scripts/prepare_chat_emojis.py`, which needs the internet.
    """
    packs = (*BUILTIN_PACKS, *custom_packs(root))
    shown = {entry['id']: browser_entry(entry, packs) for entry in select_entries(entries)}
    keep_paths = {
        Path(root, entry[pack]['src'].split('/chat-emoji/')[-1])
        for entry in shown.values() for pack in BUILTIN_PACKS
        if pack in entry and pack not in REMOTE_PACKS
    }
    removed, freed = 0, 0
    for pack in BUILTIN_PACKS:
        folder = Path(root, pack)
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.is_file() and path not in keep_paths:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
        if pack in REMOTE_PACKS and not any(folder.iterdir()):
            folder.rmdir()
    # catalog.json must not claim a file that is gone: drop the pruned entries, and
    # for the remote packs keep the source URL without the local path and hash.
    trimmed = []
    for entry in entries:
        if entry['id'] not in shown:
            continue
        record = {}
        for key, value in entry.items():
            if key in BUILTIN_PACKS:
                if key not in shown[entry['id']]:
                    continue
                if key in REMOTE_PACKS:
                    value = {'source': value['source']}
            record[key] = value
        trimmed.append(record)
    Path(root, 'catalog.json').write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'removed': removed, 'kept_local': len(keep_paths), 'freed_mib': round(freed / 1048576, 1)}


if __name__ == '__main__':
    import sys
    root = Path(__file__).resolve().parents[1] / 'static' / 'chat-emoji'
    catalog = json.loads(Path(root, 'catalog.json').read_text(encoding='utf-8'))
    catalog = merge_custom_entries(catalog, root)
    Path(root, 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    result = write_catalog_js(catalog, root)
    if '--prune' in sys.argv:
        result['pruned'] = prune_unused(catalog, root)
    print(json.dumps(result, ensure_ascii=False))
