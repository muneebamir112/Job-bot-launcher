import sys
import os

def main():
    if len(sys.argv) < 2:
        # Default to launching the UI if double-clicked
        command = "launcher"
    else:
        command = sys.argv[1]
        # Remove the command from argv so the target script sees the correct args
        sys.argv.pop(1)
    
    if command == "launcher":
        import launcher
        launcher.main()
    elif command == "fetch_jd":
        # We need to add the parent dir to sys.path so it can find resume-bot
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "resume-bot")))
        from fetch_jd import main as fetch_jd_main  # type: ignore
        fetch_jd_main()
    elif command == "ollama_generate":
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "resume-bot")))
        from ollama_generate import main as ollama_generate_main  # type: ignore
        ollama_generate_main()
    elif command == "batch_generate":
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cover_letter_bot")))
        from batch_generate import main as batch_generate_main  # type: ignore
        batch_generate_main()
    elif command == "glassdoor":
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scraper", "GlassD")))
        from glassdoor_scraper_final import main as glassdoor_main  # type: ignore
        glassdoor_main()
    elif command == "hiring_cafe":
        import asyncio
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scraper", "Hiring_cafe")))
        from scraper import main as hc_main  # type: ignore
        asyncio.run(hc_main())
    elif command == "jobgether":
        import asyncio
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scraper", "Jobgether")))
        from jobgether_scraper import main as jg_main  # type: ignore
        asyncio.run(jg_main())
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
