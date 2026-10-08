import os
import subprocess
import shutil
import sys

def main():
    root_dir = r"D:\webncodes"
    release_dir = os.path.join(root_dir, "JobBot_Pro_Release_v2")
    launcher_dir = os.path.join(root_dir, "Job-bot-launcher")
    
    print("========================================")
    print(" Job Bot Professional Release Builder")
    print("    (Monolith Entrypoint Edition)")
    print("========================================\n")
    
    # We compile ONLY jobbot_main.py. It acts as the router.
    script_path = os.path.join(launcher_dir, "jobbot_main.py")
    
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", 
        "--clean", 
        "--collect-all", "patchright", 
        "--collect-all", "playwright",
        "--windowed", # Use windowed so the launcher UI doesn't have a console. Subprocesses might need fixing if they need console, but JobBot launcher redirects stdout/stderr.
        # Add paths so PyInstaller can find the dynamically imported modules
        "--paths", os.path.join(root_dir, "resume-bot"),
        "--paths", os.path.join(root_dir, "cover_letter_bot"),
        "--paths", os.path.join(root_dir, "scraper", "GlassD"),
        "--paths", os.path.join(root_dir, "scraper", "Hiring_cafe"),
        "--paths", os.path.join(root_dir, "scraper", "Jobgether"),
        # Explicitly declare hidden imports because jobbot_main.py imports them conditionally
        "--hidden-import", "fetch_jd",
        "--hidden-import", "ollama_generate",
        "--hidden-import", "batch_generate",
        "--hidden-import", "glassdoor_scraper_final",
        "--hidden-import", "scraper",  # Hiring_cafe
        "--hidden-import", "jobgether_scraper",
        script_path
    ]
    
    print(f"[*] Compiling Monolith jobbot_main.exe...")
    result = subprocess.run(cmd, cwd=launcher_dir)
    if result.returncode != 0:
        print(f"[!] Failed to compile jobbot_main.py")
        sys.exit(1)
        
    print("\n[*] Compilation complete! Assembling release package...\n")
    
    if os.path.exists(release_dir):
        print(f"[*] Cleaning old release directory: {release_dir}")
        shutil.rmtree(release_dir, ignore_errors=True)
        
    os.makedirs(release_dir, exist_ok=True)
    
    # The output is in Job-bot-launcher/dist/jobbot_main
    # Since we didn't pass --onefile, PyInstaller creates a directory with _internal.
    # We will copy the entire dist/jobbot_main folder to JobBot_Pro_Release
    source_dist = os.path.join(launcher_dir, "dist", "jobbot_main")
    if not os.path.exists(source_dist):
        print(f"[!] Could not find compiled output at {source_dist}")
        sys.exit(1)
        
    # Copy the whole monolith folder (which contains _internal and jobbot_main.exe)
    shutil.copytree(source_dist, release_dir, dirs_exist_ok=True)
    
    # Rename jobbot_main.exe to jobbot.exe (or launcher.exe)
    old_exe = os.path.join(release_dir, "jobbot_main.exe")
    new_exe = os.path.join(release_dir, "jobbot.exe")
    if os.path.exists(old_exe):
        os.rename(old_exe, new_exe)
        print("[+] Renamed jobbot_main.exe to jobbot.exe")
        
    # We also need to recreate the empty directories the bots expect
    os.makedirs(os.path.join(release_dir, "CVs"), exist_ok=True)
    os.makedirs(os.path.join(release_dir, "resume-bot", "profiles"), exist_ok=True)
    
    # Copy the Glassdoor profile so the user stays logged in
    glassdoor_profiles_src = os.path.join(root_dir, "scraper", "GlassD", "profiles")
    if os.path.exists(glassdoor_profiles_src):
        shutil.copytree(glassdoor_profiles_src, os.path.join(release_dir, "profiles"), dirs_exist_ok=True, ignore=shutil.ignore_patterns('Cache', 'Code Cache', 'GPUCache', '*Cache*'))
        print("[+] Copied Glassdoor browser profile to release")
    
    print(f"\n[+] SUCCESS! Your highly optimized monolith release is ready at:")
    print(f"    {release_dir}")
    print("\nNext steps:")
    print("1. Distribute this folder to your clients.")
    print("2. When they run jobbot.exe launcher, the UI will open.")

if __name__ == "__main__":
    main()
