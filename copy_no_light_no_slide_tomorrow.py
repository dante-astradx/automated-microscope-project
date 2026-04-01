#!/usr/bin/env python3
"""Copy no-light and no-slide image trees with tomorrow's date in paths."""

import os
import shutil
from datetime import datetime, timedelta
import config as c

CONFIG_PY = os.path.expanduser(f"/home/{c.MICROSCOPE_USERNAME}/project_files/config.py")


def get_pi_image_dir(config_path=CONFIG_PY):
    env = {}
    with open(config_path, "r", encoding="utf-8") as f:
        code = f.read()
    exec(code, env)
    if "PI_IMAGE_DIR" not in env:
        raise RuntimeError("PI_IMAGE_DIR not found in config.py")
    return env["PI_IMAGE_DIR"]


def build_date_key(day):
    return day.strftime("%Y%m%d")


def map_path(path, old_date, new_date):
    # Replace any old_date substring in each path component with new_date.
    components = []
    for p in path.split(os.sep):
        if old_date in p:
            components.append(p.replace(old_date, new_date))
        else:
            components.append(p)
    return os.sep.join(components)


def copy_tree_replace_dates(src_root, dst_root, old_date, new_date):
    if not os.path.isdir(src_root):
        raise FileNotFoundError(f"Source folder not found: {src_root}")

    if os.path.exists(dst_root):
        raise FileExistsError(f"Destination already exists: {dst_root}")

    for dirpath, dirnames, filenames in os.walk(src_root):
        relpath = os.path.relpath(dirpath, src_root)
        if relpath == ".":
            dst_dirpath = dst_root
        else:
            dst_dirpath = os.path.join(dst_root, map_path(relpath, old_date, new_date))
        os.makedirs(dst_dirpath, exist_ok=True)

        for d in list(dirnames):
            # No action required now; os.walk handles recursion
            pass

        for fn in filenames:
            src_file = os.path.join(dirpath, fn)
            dst_filename = fn.replace(old_date, new_date) if old_date in fn else fn
            dst_file = os.path.join(dst_dirpath, dst_filename)
            shutil.copy2(src_file, dst_file)

    # For the root directory itself if old_date appears
    if old_date in os.path.basename(src_root):
        desired_root = os.path.join(os.path.dirname(src_root), os.path.basename(src_root).replace(old_date, new_date))
        if os.path.abspath(dst_root) != os.path.abspath(desired_root):
            # Already created with explicit dst_root; nothing extra required
            pass


def find_source_folders(image_dir, targets=("no-light", "no-slide")):
    candidates = []
    for entry in os.listdir(image_dir):
        path = os.path.join(image_dir, entry)
        if os.path.isdir(path) and any(entry.startswith(t) for t in targets):
            candidates.append(path)
    return sorted(candidates)


def main(overwrite=False, custom_date=None):
    pi_image_dir = get_pi_image_dir()
    today = datetime.today().date()
    if custom_date:
        # custom_date format YYYYMMDD to force date usage
        today = datetime.strptime(custom_date, "%Y%m%d").date()
    tomorrow = today + timedelta(days=1)

    old_date = build_date_key(today)
    new_date = build_date_key(tomorrow)

    print(f"PI_IMAGE_DIR = {pi_image_dir}")
    print(f"Today date token: {old_date}")
    print(f"Tomorrow date token: {new_date}")

    sources = find_source_folders(pi_image_dir)
    if not sources:
        raise RuntimeError("no-light/no-slide source folders not found in Images directory")

    for src in sources:
        base = os.path.basename(src)

        if old_date in base:
            dst_base = base.replace(old_date, new_date)
        else:
            dst_base = f"{base}_{new_date}"

        dst = os.path.join(pi_image_dir, dst_base)

        if os.path.exists(dst):
            if not overwrite:
                print(f"Skipping existing destination: {dst}")
                continue
            print(f"Overwriting existing destination: {dst}")
            shutil.rmtree(dst)

        print(f"Copying {src} -> {dst}")
        copy_tree_replace_dates(src, dst, old_date, new_date)

    print("Done")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Copy no-light/no-slide tree to tomorrow-date versions.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite destination if exists.")
    parser.add_argument("--date", type=str, help="Base date token YYYYMMDD (default today)")
    args = parser.parse_args()
    main(overwrite=args.overwrite, custom_date=args.date)
