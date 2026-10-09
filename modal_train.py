# Launch with: modal run modal_train.py
import secrets
from pathlib import Path
# pyrefly: ignore [missing-import]
import modal

# 1. Define paths
PROJECT_ROOT = Path(__file__).resolve().parent
DATASET_PATH = PROJECT_ROOT / "Dataset"
PYLOL_PATH = PROJECT_ROOT / "pyLoL"

# 2. Package cleaned CSVs into a lightweight zip (9.8 MB) so Modal doesn't scan 200,000 screenshot files on disk
ZIP_PATH = PROJECT_ROOT / "Dataset_cleaned.zip"
if not ZIP_PATH.exists():
    import zipfile
    print("Packaging cleaned CSVs into Dataset_cleaned.zip (~10MB)...")
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for csv_file in DATASET_PATH.glob("*/[BA]* data/cleaned_match_data.csv"):
            z.write(csv_file, arcname=csv_file.as_posix())

checkpoint_vol = modal.Volume.from_name("league-checkpoints-vol", create_if_missing=True)

# 3. Build container image & unpack the 10MB cleaned dataset
training_image = (
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
    # Copy and unpack the 9.8MB cleaned CSV dataset
    .add_local_file(ZIP_PATH, "/root/Dataset_cleaned.zip", copy=True)
    .run_commands("unzip -q /root/Dataset_cleaned.zip -d /root")
    # Mount pyLoL folder (notebooks, scripts)
    .add_local_dir(PYLOL_PATH, remote_path="/root/pyLoL", ignore=[".git"])
)

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
