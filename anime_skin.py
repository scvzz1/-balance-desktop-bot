"""Full-body chibi companion, drawn locally with Tk Canvas."""
import math


def draw(c, t, blink=False, mood='ok', action='idle', elapsed=0, gaze=(0, 0), expression='normal'):
    tag = 'mascot'
    def oval(box, color, outline=''):
        return c.create_oval(*box, fill=color, outline=outline, tags=tag)
    def poly(points, color, outline=''):
        return c.create_polygon(*points, fill=color, outline=outline, width=1.3, smooth=True, tags=tag)
    def line(points, color, width=2):
        return c.create_line(*points, fill=color, width=width, capstyle='round', tags=tag)
    bounce = math.sin(t*2)*1.8
    if action == 'happy':
        bounce -= abs(math.sin(elapsed*10))*9
    cx, cy = 65, 51+bounce
    hair, edge = '#B3A4EA', '#7765B2'
    oval((31,149,99,157),'#E7E3F4')
    # Long back hair and swinging side locks.
    swing = math.sin(t*2)*2
    poly((cx-29,cy-9,cx-38+swing,cy+53,cx-18,cy+69,cx-7,cy+30,cx+13,cy+67,cx+40+swing,cy+52,cx+28,cy-12),hair,edge)
    # Legs, stockings and shoes.
    for x in (cx-13,cx+11):
        poly((x-5,cy+70,x+5,cy+70,x+4,cy+94,x-4,cy+94),'#FFF0E8')
        poly((x-5,cy+80,x+5,cy+80,x+5,cy+98,x-5,cy+98),'#FAF9FF', '#D8D2EB')
        oval((x-8,cy+94,x+7,cy+101),'#54466E')
    # Dress and pleated skirt.
    poly((cx-15,cy+31,cx+15,cy+31,cx+21,cy+61,cx+30,cy+76,cx-30,cy+76,cx-20,cy+60),'#536395','#394A75')
    poly((cx-19,cy+58,cx+19,cy+58,cx+29,cy+76,cx-29,cy+76),'#7885B7')
    for dx in (-17,-6,6,17):
        line((cx+dx*.7,cy+61,cx+dx,cy+75),'#536395',1)
    line((cx-26,cy+74,cx+26,cy+74),'#DFDDF6',2)
    # Sleeves and arms: one arm waves on interaction.
    poly((cx-14,cy+31,cx-24,cy+33,cx-30,cy+50,cx-18,cy+53,cx-10,cy+37),'#F5F3FF','#D6CEE9')
    line((cx-24,cy+50,cx-26,cy+63),'#FFE4D7',7)
    if action in ('wave','happy'):
        wx = cx+34+math.sin(elapsed*15)*5
        poly((cx+12,cy+32,cx+22,cy+31,cx+29,cy+40,cx+18,cy+49),'#F5F3FF','#D6CEE9')
        line((cx+24,cy+40,wx,cy+18),'#FFE4D7',7)
        oval((wx-5,cy+10,wx+5,cy+22),'#FFE4D7')
        line((wx+8,cy+11,wx+11,cy+7),'#B3A4EA',2)
    else:
        poly((cx+14,cy+31,cx+24,cy+33,cx+30,cy+50,cx+18,cy+53,cx+10,cy+37),'#F5F3FF','#D6CEE9')
        line((cx+24,cy+50,cx+26,cy+63),'#FFE4D7',7)
    # Collar and ribbon.
    poly((cx-13,cy+30,cx,cy+43,cx+13,cy+30,cx,cy+34),'#FFFFFF')
    poly((cx,cy+39,cx-10,cy+36,cx-8,cy+47,cx,cy+42,cx+8,cy+47,cx+10,cy+36),'#EC9DB8')
    oval((cx-3,cy+38,cx+3,cy+44),'#D2779B')
    # Head follows independently from the torso; gaze remains bounded.
    gx = max(-1, min(1, gaze[0]))
    gy = max(-1, min(1, gaze[1]))
    cx += gx * 3.5
    cy += gy * 2
    # Face, hair cap, bangs and ears.
    oval((cx-29,cy-29,cx+29,cy+28),hair,edge)
    oval((cx-24,cy-19,cx+24,cy+28),'#FFF0E6')
    poly((cx-28,cy+8,cx-27,cy-24,cx-9,cy-34,cx+16,cy-31,cx+29,cy-12,cx+26,cy+12,cx+17,cy-9,cx+10,cy+1,cx+1,cy-10,cx-7,cy+1,cx-12,cy-12,cx-21,cy+10),hair,edge)
    # Star hair pin.
    star=[]
    for i in range(10):
        a=-math.pi/2+i*math.pi/5
        r=6 if i%2==0 else 2.6
        star.extend((cx+23+math.cos(a)*r,cy-15+math.sin(a)*r))
    c.create_polygon(*star,fill='#FFE4A3',outline='#DABF7F',tags=tag)
    for ex in (cx-10,cx+10):
        if blink or action=='happy' or expression in ('happy', 'shy'):
            line((ex-5,cy+8,ex,cy+5,ex+5,cy+8),'#66517E',2)
        else:
            oval((ex-5,cy+1,ex+5,cy+15),'#7963B2')
            oval((ex-2+gx*1.4,cy+5+gy,ex+2+gx*1.4,cy+12+gy),'#433259')
            oval((ex-3+gx,cy+7+gy,ex+2+gx,cy+13+gy),'#B0A3ED')
            oval((ex-3+gx,cy+2+gy,ex+1+gx,cy+6+gy),'#FFFFFF')
            line((ex-5,cy+1,ex+5,cy+1),'#554363',2)
    oval((cx-21,cy+13,cx-11,cy+18),'#F5BDCC')
    oval((cx+11,cy+13,cx+21,cy+18),'#F5BDCC')
    if expression == 'shy':
        for side in (-1, 1):
            for dx in (-3, 0, 3):
                x = cx + side*16 + dx
                line((x-1,cy+13,x+1,cy+17),'#DD88A3',1)
    if expression == 'surprised':
        oval((cx-3,cy+19,cx+3,cy+26),'#B37988')
        for ex in (cx-10,cx+10):
            line((ex-4,cy-4,ex+4,cy-5),'#77608C',1.5)
    elif mood=='error' or expression == 'worried':
        line((cx-4,cy+22,cx,cy+20,cx+4,cy+22),'#B37988',1.5)
    else:
        line((cx-4,cy+20,cx,cy+23,cx+4,cy+20),'#B37988',1.5)
