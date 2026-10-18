import os
import zipfile

# Nazwa wtyczki (taki sam będzie folder główny wewnątrz pliku ZIP)
PLUGIN_NAME = "QMapAnalyst"
ZIP_FILENAME = f"{PLUGIN_NAME}.zip"

# Czego NIE pakować do paczki ZIP
EXCLUDE_DIRS = ["__pycache__", ".git", ".idea", ".vscode"]
EXCLUDE_FILES = [ZIP_FILENAME, "build.py", ".gitignore", "resources.qrc", "resources.py"]
EXCLUDE_EXTS = [".pyc", ".pyo"]

def build_plugin():
    source_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(source_dir)
    zip_path = os.path.join(parent_dir, ZIP_FILENAME)

    print(f"📦 Rozpoczynam pakowanie wtyczki {PLUGIN_NAME}...")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(source_dir):
            
            # Filtrowanie folderów (usuwamy cache i ukryte foldery z procesu)
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith('.')]

            for file in files:
                # Filtrowanie plików
                if file in EXCLUDE_FILES or file.startswith('.'):
                    continue
                if any(file.endswith(ext) for ext in EXCLUDE_EXTS):
                    continue
                
                # Pełna ścieżka do pliku na dysku
                file_path = os.path.join(root, file)
                
                # Relatywna ścieżka dla struktury w pliku ZIP (musi zaczynać się od QMapAnalyst/...)
                rel_path = os.path.relpath(file_path, source_dir)
                zip_arcname = os.path.join(PLUGIN_NAME, rel_path)
                
                # Zapis pliku do archiwum
                zipf.write(file_path, zip_arcname)
                print(f"  + Dodano: {zip_arcname}")

    print(f"\n✅ Sukces! Plik .zip jest gotowy do publikacji w repozytorium QGIS.")
    print(f"📂 Zapisano w: {zip_path}")

if __name__ == "__main__":
    build_plugin()