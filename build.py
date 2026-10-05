import os
import subprocess
import shutil
import sys

def run_pyinstaller(script_path, is_windowed=False):
    print(f"[*] Compiling {script_path}...")
    
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--collect-all", "patchright"]
    if is_windowed:
        cmd.append("--windowed")
        
    cmd.append(script_path)
    
    work_dir = os.path.dirname(script_path)
    script_name = os.path.basename(script_path)
    
    result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[!] Failed to compile {script_name}:\n{result.stderr}")
        return False
    return True

def copy_exe_to_release(source_dir, script_name, dest_dir, rename_to=None):
    exe_name = script_name.replace(".py", ".exe")
    source_exe = os.path.join(source_dir, "dist", exe_name)
    
    if not os.path.exists(source_exe):
        print(f"[!] Could not find compiled {exe_name} at {source_exe}")
        return False
        
    os.makedirs(dest_dir, exist_ok=True)
    
    final_name = rename_to if rename_to else exe_name
    dest_exe = os.path.join(dest_dir, final_name)
    
    shutil.copy2(source_exe, dest_exe)
    print(f"[+] Successfully packaged: {final_name}")
    return True

def main():
    root_dir = r"D:\webncodes"
    release_dir = os.path.join(root_dir, "JobBot_Pro_Release")
    
    print("========================================")
    print(" Job Bot Professional Release Builder")
    print("========================================\n")
    
    targets = [
        (r"scraper\GlassD\glassdoor_scraper_final.py", False),
        (r"scraper\Hiring_cafe\scraper.py", False),
        (r"scraper\Jobgether\jobgether_scraper.py", False),
        (r"resume-bot\fetch_jd.py", False),
        (r"resume-bot\ollama_generate.py", False),
        (r"Job-bot-launcher\launcher.py", True)
    ]
    
    for rel_path, is_win in targets:
        full_path = os.path.join(root_dir, rel_path)
        if not os.path.exists(full_path):
            print(f"[!] Missing source file: {full_path}")
            continue
        run_pyinstaller(full_path, is_windowed=is_win)
        
    print("\n[*] Compilation complete! Assembling release package...\n")
    
    if os.path.exists(release_dir):
        print(f"[*] Cleaning old release directory: {release_dir}")
        shutil.rmtree(release_dir)
        
    os.makedirs(release_dir, exist_ok=True)
    
    copy_exe_to_release(os.path.join(root_dir, "scraper", "GlassD"), "glassdoor_scraper_final.py", os.path.join(release_dir, "scraper", "GlassD"))
    copy_exe_to_release(os.path.join(root_dir, "scraper", "Hiring_cafe"), "scraper.py", os.path.join(release_dir, "scraper", "Hiring_cafe"))
    copy_exe_to_release(os.path.join(root_dir, "scraper", "Jobgether"), "jobgether_scraper.py", os.path.join(release_dir, "scraper", "Jobgether"))
    
    copy_exe_to_release(os.path.join(root_dir, "resume-bot"), "fetch_jd.py", os.path.join(release_dir, "resume-bot"))
    copy_exe_to_release(os.path.join(root_dir, "resume-bot"), "ollama_generate.py", os.path.join(release_dir, "resume-bot"))
    
    copy_exe_to_release(os.path.join(root_dir, "Job-bot-launcher"), "launcher.py", os.path.join(release_dir, "Job-bot-launcher"))
    
    
    
    print(f"\n[+] SUCCESS! Your highly secure, source-code-free release is ready at:")
    print(f"    {release_dir}")
    print("\nNext steps:")
    print("1. Distribute this folder to your clients.")
    print("2. When they run launcher.exe, the Configuration Wizard will walk them through adding their Google Sheet and Service Account.")

if __name__ == "__main__":
    main()
