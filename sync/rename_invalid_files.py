import os
import re
import sys

# Characters not allowed in filenames (NTFS and cross-platform safe)
#INVALID_CHARS = r'":<>|*?\r\n'

INVALID_CHARS = ['"', ':', '<', '>', '|', '*', '?', '\r', '\n']

def has_invalid_chars(name: str) -> bool:
    """Check if filename contains any invalid characters."""
    return any(c in name for c in INVALID_CHARS)

def clean_filename(name: str) -> str:
    """Remove invalid characters from a filename, preserving extension."""
    # Separate the extension from the stem
    stem, *ext_parts = name.rsplit(".", 1)
    clean_stem = re.sub(f"[{re.escape(INVALID_CHARS)}]", "", stem).strip()
    
    if ext_parts:
        clean_ext = re.sub(f"[{re.escape(INVALID_CHARS)}]", "", ext_parts[0]).strip()
        return f"{clean_stem}.{clean_ext}" if clean_stem else ""
    return clean_stem

def rename_and_cleanup(root_dir: str = "."):
    renamed = []
    deleted = []
    skipped = []

    # Collect all files with invalid characters
    invalid_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if has_invalid_chars(filename):
                invalid_files.append((dirpath, filename))

    if not invalid_files:
        print("No files with invalid characters found.")
        return

    print(f"Found {len(invalid_files)} file(s) with invalid characters:\n")

    for dirpath, filename in invalid_files:
        old_path = os.path.join(dirpath, filename)
        new_filename = clean_filename(filename)

        print(f"  Original : {old_path}")

        # Case 1: Cleaned name is empty — delete the file
        if not new_filename:
            os.remove(old_path)
            print(f"  Action   : [DELETED] (filename would be empty after cleaning)\n")
            deleted.append(old_path)
            continue

        new_path = os.path.join(dirpath, new_filename)

        # Case 2: Cleaned file already exists — delete the original special char file
        if os.path.exists(new_path):
            os.remove(old_path)
            print(f"  Cleaned  : {new_path}")
            print(f"  Action   : [DELETED] original (cleaned version already exists)\n")
            deleted.append(old_path)
            continue

        # Case 3: Safe to rename
        #os.rename(old_path, new_path)
        shutil.copy2(old_path, new_path)  # copy with metadata
        os.remove(old_path)               # explicitly delete original
        print(f"  Cleaned  : {new_path}")
        print(f"  Action   : [RENAMED]\n")
        renamed.append((old_path, new_path))

    # Summary
    print("=" * 60)
    print(f"Summary:")
    print(f"  Renamed  : {len(renamed)} file(s)")
    print(f"  Deleted  : {len(deleted)} file(s)")
    print(f"  Skipped  : {len(skipped)} file(s)")
    print("=" * 60)

    if renamed:
        print("\nRenamed files:")
        for old, new in renamed:
            print(f"  {old}  ->  {new}")

    if deleted:
        print("\nDeleted files:")
        for f in deleted:
            print(f"  {f}")

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."

    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a valid directory.")
        sys.exit(1)

    print(f"Scanning directory: {os.path.abspath(root)}\n")
    rename_and_cleanup(root)
