# Updating Meridian (Windows)

Releases are versioned: `meridian_v1.5.1.zip`, `meridian_v1.6.0.zip`, ...
The current installed version shows in the app header and in the `VERSION` file.

## Your data is always preserved
Updates replace **code** but keep your personal files:
`resume_master.yaml` (CV), `config.yaml` (settings), `secrets.yaml` (API key),
`jobcopilot.db` (jobs/applications/usage), and `outputs/` (generated resumes).
A timestamped `_backup_...` folder is also created each time, just in case.

## How to update
1. Download the new `jobcopilot_v*.zip` into your **Downloads** folder.
2. Open **PowerShell** and run the updater (it auto-picks the newest zip in Downloads):
   ```powershell
   cd "C:\Users\GenAI\Documents\Gen AI Projects\jobcopilot"
   .\update.ps1
   ```
3. Then refresh dependencies and launch:
   ```powershell
   venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   streamlit run app.py
   ```

> First time only (you don't have `update.ps1` yet): use the one-time PowerShell block from
> the release notes; it installs `update.ps1` into your project for all future updates.
