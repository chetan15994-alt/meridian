# How to Run Meridian

## Run it

```powershell
cd "C:\Users\GenAI\Documents\Gen AI Projects\jobcopilot"
venv\Scripts\Activate.ps1
pip install -r requirements.txt

python run.py              # discover + score jobs
streamlit run app.py       # open the app at localhost:8501
```

## One-time: connect this folder to GitHub

```powershell
cd "C:\Users\GenAI\Documents\Gen AI Projects\jobcopilot"
git init
git remote add origin https://github.com/chetan15994-alt/meridian.git
git add -A
git commit -m "v1.17.0"
git branch -M main
git push -u origin main
```

`.gitignore` (included in every build) keeps your resume, config, API key, and database out of GitHub — make sure it's in the folder before `git add -A`.

## Every time a new build is released

1. Download the new `meridian_vX.Y.Z.zip` to your **Downloads** folder.
2. Apply it:
   ```powershell
   cd "C:\Users\GenAI\Documents\Gen AI Projects\jobcopilot"
   .\update.ps1
   ```
3. Push it to GitHub:
   ```powershell
   git add -A
   git commit -m "vX.Y.Z"
   git push
   ```

## Pulling

```powershell
cd "C:\Users\GenAI\Documents\Gen AI Projects\jobcopilot"
git pull
```
