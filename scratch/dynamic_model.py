import sys

path = r'd:\webncodes\Job-bot-launcher\launcher.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

bad = '''    tk.Label(input_frame, text="Model Name:", font=("Segoe UI", 10), bg=panel_bg, fg=fg_color).grid(row=0, column=0, sticky="w", pady=5)
    model_entry = tk.Entry(input_frame, width=25, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 11), relief="flat")
    model_entry.grid(row=0, column=1, sticky="w", padx=10, pady=5, ipady=4)
    model_entry.insert(0, env_dict.get("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    
    tk.Label(input_frame, text="Host URL:", font=("Segoe UI", 10), bg=panel_bg, fg=fg_color).grid(row=1, column=0, sticky="w", pady=5)
    host_entry = tk.Entry(input_frame, width=35, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 11), relief="flat")
    host_entry.grid(row=1, column=1, sticky="w", padx=10, pady=5, ipady=4)
    host_entry.insert(0, env_dict.get("OLLAMA_HOST", "http://localhost:11434"))'''

good = '''    tk.Label(input_frame, text="Host URL:", font=("Segoe UI", 10), bg=panel_bg, fg=fg_color).grid(row=1, column=0, sticky="w", pady=5)
    host_entry = tk.Entry(input_frame, width=35, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 11), relief="flat")
    host_entry.grid(row=1, column=1, columnspan=2, sticky="w", padx=10, pady=5, ipady=4)
    host_entry.insert(0, env_dict.get("OLLAMA_HOST", "http://localhost:11434"))

    tk.Label(input_frame, text="Model Name:", font=("Segoe UI", 10), bg=panel_bg, fg=fg_color).grid(row=0, column=0, sticky="w", pady=5)
    model_entry = ttk.Combobox(input_frame, width=23, font=("Consolas", 11))
    model_entry.grid(row=0, column=1, sticky="w", padx=10, pady=5, ipady=4)
    
    def refresh_models():
        try:
            import urllib.request, json
            host_url = host_entry.get().strip().rstrip('/')
            req = urllib.request.Request(f"{host_url}/api/tags")
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode())
                models = [m['name'] for m in data.get('models', [])]
                if models:
                    model_entry['values'] = models
                    if model_entry.get() not in models:
                        model_entry.set(models[0])
        except Exception:
            pass
            
    refresh_btn = tk.Button(input_frame, text="🔄 Fetch", command=refresh_models, bg="#555", fg="white", relief="flat", font=("Segoe UI", 9))
    refresh_btn.grid(row=0, column=2, padx=5)
    
    default_model = env_dict.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
    model_entry.set(default_model)
    model_entry['values'] = [default_model]
    host_entry.after(500, refresh_models)'''

if bad in content:
    content = content.replace(bad, good)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Successfully replaced model entry with Combobox")
else:
    print("Could not find the target code in launcher.py")
