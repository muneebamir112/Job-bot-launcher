import asyncio
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://hiringcafe.com/', wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        
        await page.evaluate("""() => {
            const editBtn = document.querySelector('button.flex.items-center.space-x-4');
            if (editBtn) editBtn.click();
        }""")
        await page.wait_for_timeout(2000)

        await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
            const remoteBtn = buttons.find(b => b.innerText && b.innerText.toLowerCase() === 'remote');
            if (remoteBtn) remoteBtn.click();
        }""")
        await page.wait_for_timeout(2000)

        print('URL:', page.url)
        
        await browser.close()

asyncio.run(main())
