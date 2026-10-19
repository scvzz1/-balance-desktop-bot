"""Cached PNG sprite renderer; runtime uses only standard tkinter."""
import math
from pathlib import Path
import tkinter as tk

NAMES = {
    'pastel_cat':'蜜桃猫耳', 'pastel_mage':'星愿法师',
    'pastel_cyber':'霓虹猫耳', 'pastel_miko':'樱铃巫女',
    'pastel_hat':'晴日花语', 'pastel_witch':'暮星魔女',
}
FRAMES = Path(__file__).with_name('character_sprites')

def draw(c, skin, t, blink, mood, action, elapsed, gaze, expression):
    cache = getattr(c, '_pastel_frames', None)
    if cache is None:
        cache = c._pastel_frames = {}
    gx,gy = gaze
    if max(abs(gx),abs(gy)) < .2:
        direction='center'
    elif abs(gx)>=abs(gy):
        direction='right' if gx>0 else 'left'
    else:
        direction='down' if gy>0 else 'up'
    exp = 'blink' if blink else expression
    if exp not in ('normal','happy','shy','surprised','worried','blink'):
        exp='normal'
    key=(skin,exp,direction)
    if key not in cache:
        cache[key]=tk.PhotoImage(master=c,file=str(FRAMES/f'{skin}-{exp}-{direction}.png'))
    y=8+math.sin(t*2)*1.8
    if action=='happy':y-=abs(math.sin(elapsed*10))*6
    c.create_image(0,y,anchor='nw',image=cache[key],tags='mascot')
    if action in ('wave','happy'):
        c.create_text(112,24+math.sin(elapsed*5)*3,text='✧',fill='#D987B4',font=('Segoe UI',16),tags='mascot')
