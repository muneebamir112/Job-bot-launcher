import os

with open(r'D:\webncodes\Job-bot-launcher\launcher.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_logic = '''                    shutil.copy(fp, dest1)
                    shutil.copy(fp, dest2)'''

new_logic = '''                    if os.path.abspath(fp) != os.path.abspath(dest1):
                        shutil.copy(fp, dest1)
                    if os.path.abspath(fp) != os.path.abspath(dest2):
                        shutil.copy(fp, dest2)'''

if old_logic in text:
    text = text.replace(old_logic, new_logic)
    with open(r'D:\webncodes\Job-bot-launcher\launcher.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print('Patched upload logic!')
else:
    print('Could not find old logic in launcher.py')
