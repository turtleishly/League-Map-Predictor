\"\"\"
Helper utility to package the dataset for Modal cloud training.
Creates:
1. Dataset_cleaned.zip (~10 MB): All cleaned CSVs, player_states.json, and roles.json
2. Dataset_images_64.zip (~150 MB): All minimap frames downsampled to 64x64 JPEGs for rapid cloud transfer

Usage:
    python pyLoL/pack_dataset.py
\"\"\"
import time
import zipfile
from pathlib import Path
import cv2
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = PROJECT_ROOT / "Dataset"
CLEANED_ZIP = PROJECT_ROOT / "Dataset_cleaned.zip"
IMAGES_ZIP  = PROJECT_ROOT / "Dataset_images_64.zip"

def pack_metadata():
    print(f"📦 Packaging CSVs, player_states.json, and roles.json into {CLEANED_ZIP.name}...")
    t0 = time.time()
    count = 0
    with zipfile.ZipFile(CLEANED_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in DATASET_PATH.glob("*/[BA]* data/cleaned_match_data.csv"):
            z.write(f, arcname=f.relative_to(PROJECT_ROOT).as_posix())
            count += 1
        for f in DATASET_PATH.glob("*/player_states.json"):
            z.write(f, arcname=f.relative_to(PROJECT_ROOT).as_posix())
            count += 1
        for f in DATASET_PATH.glob("*/roles.json"):
            z.write(f, arcname=f.relative_to(PROJECT_ROOT).as_posix())
            count += 1
    t1 = time.time()
    size_mb = CLEANED_ZIP.stat().st_size / (1024 * 1024)
    print(f"✓ Packaged {count} metadata files in {t1 - t0:.2f}s ({size_mb:.2f} MB)")

def pack_images(img_size=64, quality=85):
    print(f"\n🖼️ Downsampling minimap screenshots to {img_size}x{img_size} into {IMAGES_ZIP.name}...")
    matches = sorted([m for m in DATASET_PATH.iterdir() if m.is_dir() and not m.name.startswith("_")])
    t0 = time.time()
    total_imgs = 0

    with zipfile.ZipFile(IMAGES_ZIP, "w", compression=zipfile.ZIP_STORED) as z:
        for m in tqdm(matches, desc="Matches"):
            blue_dir = m / "0" / "Blue"
            if not blue_dir.exists():
                continue
            for p in blue_dir.glob("*.png"):
                im = cv2.imread(str(p))
                if im is not None:
                    im_resized = cv2.resize(im, (img_size, img_size))
                    _, enc = cv2.imencode(".jpg", im_resized, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    arcname = f"Dataset/{m.name}/0/Blue/{p.stem}.jpg"
                    z.writestr(arcname, enc.tobytes())
                    total_imgs += 1

    t1 = time.time()
    size_mb = IMAGES_ZIP.stat().st_size / (1024 * 1024)
    print(f"✓ Downsampled and packaged {total_imgs:,} images in {t1 - t0:.1f}s ({size_mb:.2f} MB)")

if __name__ == "__main__":
    pack_metadata()
    pack_images()
    print("\n🎉 Dataset packaging complete! Ready for Modal run.")
