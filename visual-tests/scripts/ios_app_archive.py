"""Inspect iOS app archives (.ipa, or a .zip holding a .app) without unpacking.

Archives stay intact on the Linux side and are unpacked only on the Mac, where
ditto preserves the symlinks and exec bits that the code signature depends on.
"""
import plistlib
import re
import zipfile

IOS_ARCHIVE_EXTENSIONS = (".ipa", ".zip")
# First ".app/" path segment followed directly by Info.plist. The non-greedy
# prefix means plists nested deeper (frameworks, extensions, watch apps) don't
# match as the app's own.
_APP_PLIST = re.compile(r"^((?:[^/]+/)*?[^/]+\.app)/Info\.plist$")


def is_ios_archive(path):
    return str(path).lower().endswith(IOS_ARCHIVE_EXTENSIONS)


def find_app_plist(names):
    """Return the entry name of the top-level app's Info.plist, or None."""
    matches = []
    for name in names:
        match = _APP_PLIST.match(name)
        if match and ".app/" not in match.group(1):
            matches.append(name)
    return min(matches, key=lambda n: (n.count("/"), n)) if matches else None


def read_bundle_id(archive_path):
    """Return CFBundleIdentifier from an .ipa/.zip. Raises ValueError if absent."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entry = find_app_plist(archive.namelist())
            if not entry:
                raise ValueError(f"no .app bundle found in {archive_path}")
            info = plistlib.loads(archive.read(entry))
    except (zipfile.BadZipFile, plistlib.InvalidFileException, OSError) as e:
        raise ValueError(f"unreadable iOS app archive {archive_path}: {e}") from e
    bundle_id = info.get("CFBundleIdentifier")
    if not bundle_id:
        raise ValueError(f"{entry} in {archive_path} has no CFBundleIdentifier")
    return bundle_id
