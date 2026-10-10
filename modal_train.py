# Launch with: modal run modal_train.py
import secrets
from pathlib import Path
# pyrefly: ignore [missing-import]
import modal

# 1. Define paths
PROJECT_ROOT = Path(__file__).resolve().parent
DATASET_PATH = PROJECT_ROOT / "Dataset"
PYLOL_PATH = PROJECT_ROOT / "pyLoL"

# 2. Package cleaned dataset (CSVs + player_states + roles) into a lightweight zip (10.3 MB)
ZIP_PATH = PROJECT_ROOT / "Dataset_cleaned.zip"
IMAGES_ZIP_PATH = PROJECT_ROOT / "Dataset_images_64.zip"

def should_rebuild_zip(z_path):
    if not z_path.exists():
        return True
    import zipfile
    try:
        with zipfile.ZipFile(z_path, "r") as z:
            names = z.namelist()
            return not (any("player_states.json" in n for n in names) and any("roles.json" in n for n in names))
    except Exception:
        return True

if should_rebuild_zip(ZIP_PATH):
    import zipfile
    print("Packaging cleaned CSVs, player_states.json, and roles.json into Dataset_cleaned.zip (~10MB)...")
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for csv_file in DATASET_PATH.glob("*/[BA]* data/cleaned_match_data.csv"):
            z.write(csv_file, arcname=csv_file.relative_to(PROJECT_ROOT).as_posix())
        for state_file in DATASET_PATH.glob("*/player_states.json"):
            z.write(state_file, arcname=state_file.relative_to(PROJECT_ROOT).as_posix())
        for role_file in DATASET_PATH.glob("*/roles.json"):
            z.write(role_file, arcname=role_file.relative_to(PROJECT_ROOT).as_posix())

checkpoint_vol = modal.Volume.from_name("league-checkpoints-vol", create_if_missing=True)

# 3. Build container image & unpack datasets
image_builder = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("unzip")
    .pip_install(
        "jupyterlab",
        "torch",
        "torchvision",
        "pandas",
        "numpy",
        "matplotlib",
        "tqdm",
        "opencv-python-headless"
    )
    # Copy and unpack the 10.3MB cleaned CSV + metadata dataset
    .add_local_file(ZIP_PATH, "/root/Dataset_cleaned.zip", copy=True)
    .run_commands("unzip -q -o /root/Dataset_cleaned.zip -d /root")
)

# If 64x64 minimap images zip exists, copy and unpack to /root/Dataset
if IMAGES_ZIP_PATH.exists():
    print(f"Adding pre-packaged 64x64 visual minimap dataset ({IMAGES_ZIP_PATH.stat().st_size / (1024*1024):.1f} MB)...")
    image_builder = (
        image_builder
        .add_local_file(IMAGES_ZIP_PATH, "/root/Dataset_images_64.zip", copy=True)
        .run_commands("unzip -q -o /root/Dataset_images_64.zip -d /root")
    )

training_image = image_builder.add_local_dir(PYLOL_PATH, remote_path="/root/pyLoL", ignore=[".git"])

app = modal.App("league-map-predictor-training", image=training_image)


@app.local_entrypoint()
def main():
    print("🚀 Spinning up Modal GPU environment for League Map Predictor...")
    token = secrets.token_urlsafe(16)

    # 4. Launch remote Jupyter Lab sandbox
    # GPU options: "T4" (super cheap), "L4" (very fast & cheap), "A10G", or "A100"
    sandbox = modal.Sandbox.create(
        "jupyter", "lab",
        "--no-browser",
        "--ip=0.0.0.0",
        "--port=8888",
        "--allow-root",
        f"--ServerApp.token={token}",
        "--ServerApp.disable_check_xsrf=True",
        "--ServerApp.allow_origin='*'",
        image=training_image,
        gpu="T4",  # A T4 or L4 is easily 5x-10x faster than a laptop 3050 Ti and costs pennies/hr!
        encrypted_ports=[8888],
        timeout=7200,  # 2 hours auto-shutdown safety net
        app=app,
        workdir="/root",
        volumes={
            "/root/models": checkpoint_vol,  # Saved models persist here permanently
        },
    )

    tunnel_url = sandbox.tunnels()[8888].url
    print("\n" + "="*80)
    print("✅ SUCCESS! Your Cloud GPU Jupyter Server is ready.")
    print("🔗 COPY THIS URL into VS Code or your browser:")
    print(f"{tunnel_url}/lab?token={token}")
    print("="*80 + "\n")
    print("💡 Note: Models saved to '/root/models' will persist in your Modal volume permanently.\n")

    try:
        sandbox.wait()
    except KeyboardInterrupt:
        print("Stopping remote server...")
        sandbox.terminate()
