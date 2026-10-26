"""Rebuild desktop icon assets. Uses development Pillow + Playwright/Chromium only.
Run from any directory: .venv/bin/python assets/branding/build_icons.py
"""
import asyncio
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width':1024,'height':1024},device_scale_factor=1)
        svg = (HERE/'app-icon.svg').read_text(encoding='utf-8')
        await page.set_content('<style>html,body{margin:0;background:transparent}svg{display:block}</style>'+svg)
        await page.screenshot(path=str(HERE/'app-icon-1024.png'),omit_background=True)
        await browser.close()
    master=Image.open(HERE/'app-icon-1024.png').convert('RGBA')
    master.save(ROOT/'icon_macos.png')
    master.save(ROOT/'icon.icns',format='ICNS')
    sizes=[(n,n) for n in (16,20,24,32,40,48,64,96,128,256)]
    master.save(ROOT/'icon.ico',format='ICO',sizes=sizes)
    master.resize((512,512),Image.Resampling.LANCZOS).save(HERE/'app-icon-preview.png')
    board=Image.new('RGB',(1440,880),'#F1F3EC');d=ImageDraw.Draw(board)
    font_path='/System/Library/Fonts/Supplemental/Arial.ttf'
    def font(size):
        try:return ImageFont.truetype(font_path,size)
        except OSError:return ImageFont.load_default(size=size)
    d.text((64,46),'VOCAB HARVESTER',font=font(22),fill='#315B4C')
    d.text((62,85),'Gather.',font=font(66),fill='#143D34')
    d.text((65,174),'Desktop icon / 2026',font=font(20),fill='#64766B')
    board.paste(master.resize((520,520),Image.Resampling.LANCZOS),(54,238),master.resize((520,520),Image.Resampling.LANCZOS))
    for y,bg,fg,label in [(244,'#FFFFFF','#405B50','LIGHT DESKTOP'),(514,'#152523','#CCDED4','DARK DESKTOP')]:
        d.rounded_rectangle((640,y,1376,y+230),radius=24,fill=bg)
        d.text((668,y+24),label,font=font(15),fill=fg)
        x=680
        for n in (128,64,48,32,16):
            tile=master.resize((n,n),Image.Resampling.LANCZOS)
            board.paste(tile,(x,y+75+(128-n)//2),tile)
            d.text((x,y+205),str(n)+' px',font=font(14),fill=fg)
            x+=n+42
    d.text((66,817),'Emerald / Ivory / Warm gold     |     Text + images, gathered into one place.',font=font(20),fill='#64766B')
    board.save(HERE/'app-icon-presentation.png')
    with Image.open(ROOT/'icon.ico') as ico:
        assert ico.ico.sizes()==set(sizes)
        for size in sizes: assert ico.ico.getimage(size).size==size
    with Image.open(ROOT/'icon.icns') as icns:
        icns.load();assert icns.size==(1024,1024)
    assert master.getpixel((0,0))[3]==0
    print('PASS: transparent master, 10 Windows sizes, macOS ICNS and presentation generated')

if __name__=='__main__':asyncio.run(main())
