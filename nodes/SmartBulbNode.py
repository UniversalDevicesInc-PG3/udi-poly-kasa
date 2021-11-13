#
# TP Link Kasa Smart Bulb Node
#
# This code is used for bulbs
#
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartBulb,SmartDeviceException
from nodes import SmartDeviceNode
from converters import color_hsv, color_rgb, bri2st, st2bri, rgb2hsv

# TODO: Use min/max values to get correct NodeDef
# SmartDeviceNode:set_state_a: exit:  dev=<DeviceType.Bulb model KL120(US) at 192.168.86.144 (Test KL120), is_on: True - dev specific: {'Brightness': 50, 'Is dimmable': True, 'Color temperature': 2700, 'Valid temperature range': ColorTempRange(min=2700, max=5000)}>
# TODO: set_light_state allows setting transition time?

class SmartBulbNode(SmartDeviceNode):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        self.name = name
        self.debug_level = 0
        self.drivers = [
            {'driver': 'ST', 'value': 0, 'uom': 51},
            {'driver': 'GV0', 'value': 0, 'uom': 2},  #connection state
            {'driver': 'GV5', 'value': 0, 'uom': 100}, #brightness
        ]
        if dev is not None:
            # Figure out the id based in the device info
            self.id = 'SmartBulb_'
            if dev.is_dimmable:
                self.id += 'D'
            else:
                self.id += 'N'
            if dev.is_variable_color_temp:
                self.id += 'T'
            else:
                self.id += 'N'
            if dev.is_color:
                self.id += 'C'
            else:
                self.id += 'N'
            if dev.has_emeter:
                self.id += 'E'
            else:
                self.id += 'N'
            cfg['emeter'] = dev.has_emeter
            cfg['color']  = dev.is_color
            cfg['color_temp'] = dev.is_variable_color_temp
        else:
            self.id = cfg['id']
        if cfg['color_temp']:
            self.drivers.append({'driver': 'CLITEMP', 'value': 0, 'uom': 26})
        if cfg['color']:
            self.drivers.append({'driver': 'GV3', 'value': 0, 'uom': 100}) #hue
            self.drivers.append({'driver': 'GV4', 'value': 0, 'uom': 100}) #sat
        if cfg['emeter']:
            self.drivers.append({'driver': 'CPW', 'value': 0, 'uom': 73})            
        super().__init__(controller, primary, address, name, dev, cfg)

    async def set_bri_a(self,val):
        self.setDriver('GV5',val)
        # This won't actually change unless the device is on?
        if not self.dev.is_on:
            await self.dev.turn_on()
        await self.dev.set_brightness(int(bri2st(val)))
        await self.set_state_a() # TODO: Should we really set all states, or just call update?
        self.setDriver('ST',self.dev.brightness)

    async def brt_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        await self.dev.update()
        self.brightness = st2bri(self.dev.brightness)
        if self.brightness <= 255:
            nv = self.brightness + 15
            if nv > 255:
                nv = 255
            await self.set_bri_a(nv)
        LOGGER.debug(f'{self.pfx} exit')

    async def dim_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        await self.dev.update()
        self.brightness = st2bri(self.dev.brightness)
        if self.brightness > 0:
            nv = self.brightness - 15
            if nv < 0:
                nv = 0
            await self.set_bri_a(nv)
        LOGGER.debug(f'{self.pfx} exit')

    async def set_hue_a(self,val):
        LOGGER.debug(f'{self.pfx} val={val}')
        await self.dev.update()
        hsv = list(self.dev.hsv)
        await self.dev.set_hsv(hue=val, saturation=hsv[1], value=hsv[2])
        self.setDriver('GV3',val)
        await self.set_state_a()

    async def set_sat_a(self,val):
        LOGGER.debug(f'{self.pfx} val={val}')
        await self.dev.update()
        hsv = list(self.dev.hsv)
        await self.dev.set_hsv(hue=hsv[0], saturation=bri2st(val), value=hsv[2])
        self.setDriver('GV4',st2bri(val))
        await self.set_state_a()

    async def set_color_temp_a(self,val):
        LOGGER.debug(f'{self.pfx} val={val}')
        if not self.dev.is_variable_color_temp:
            LOGGER.error('{self.pfx} Color Temperature Not supported on this device?')
            return False
        self.setDriver('CLITEMP',self.dev.color_temp)
        await self.dev.set_color_temp(int(val))
        await self.set_state_a()

    async def set_color_temp_brightness_a(self,color_temp,brightness,duration):
        LOGGER.debug(f'{self.pfx} color_temp={color_temp} brightness={brightness} duration={duration}')
        if not self.dev.is_variable_color_temp:
            LOGGER.error(f'{self.pfx} Color Temperature Not supported on this device?')
            return False
        light_state = await self.dev.get_light_state()
        LOGGER.debug(f'{self.pfx} current_state={light_state}')
        light_state['on_off'] = 1
        light_state['brightness'] = bri2st(brightness)
        if color_temp < self.dev.valid_temperature_range[0]:
            LOGGER.error(f'{self.pfx} color_temp={color_temp} is to low, using minimum {self.dev.valid_temperature_range[0]}')
            color_temp = self.dev.valid_temperature_range[0]
        elif color_temp > self.dev.valid_temperature_range[1]:
            LOGGER.error(f'{self.pfx} color_temp={color_temp} is to high, using maximum {self.dev.valid_temperature_range[1]}')
            color_temp = self.dev.valid_temperature_range[1]
        light_state['color_temp'] = color_temp
        LOGGER.debug(f'{self.pfx}     new_state={light_state}')
        try:
            await self.dev.set_light_state(light_state)
        except SmartDeviceException as ex:
            LOGGER.error(f'{self.pfx} failed: {ex}')
        await self.set_state_a()

    async def set_color_name_a(self,val):
        LOGGER.debug(f'{self.pfx} val={val}')
        rgb = color_rgb(val)
        hsv = color_hsv(val)
        LOGGER.debug(f'set_color_name rgb={rgb} hsv={hsv}')
        await self.dev.set_hsv(hue=hsv[0], saturation=hsv[1], value=hsv[2])
        await self.set_state_a()

    async def set_color_rgb_a(self,red,green,blue,brightness,duration):
        LOGGER.debug(f'{self.pfx} red={red} green={green} blue={blue} brightness={brightness} duration={duration}')
        if not self.dev.is_color:
            LOGGER.error(f'{self.pfx} Color not supported on this device?')
            return False
        hsv = rgb2hsv(red,green,blue)
        light_state = await self.dev.get_light_state()
        LOGGER.debug(f'{self.pfx} current_state={light_state}')
        light_state['on_off'] = 1
        light_state['brightness'] = bri2st(brightness)
        light_state['transition'] = duration
        LOGGER.debug(f'{self.pfx}     new_state={light_state}')
        try:
            await self.dev.set_light_state(light_state)
        except SmartDeviceException as ex:
            LOGGER.error(f'{self.pfx} failed: {ex}')
        await self.dev.set_hsv(hue=hsv[0], saturation=hsv[1], value=hsv[2])
        await self.set_state_a()

    async def set_color_hsv_a(self,hue,saturation,brightness,duration):
        LOGGER.debug(f'{self.pfx} hue={hue} saturation={saturation} brightness={brightness} duration={duration}')
        if not self.dev.is_color:
            LOGGER.error(f'{self.pfx} Color not supported on this device?')
            return False
        light_state = await self.dev.get_light_state()
        LOGGER.debug(f'{self.pfx} current_state={light_state}')
        light_state['on_off'] = 1
        light_state['brightness'] = bri2st(brightness)
        light_state['transition'] = duration
        LOGGER.debug(f'{self.pfx}     new_state={light_state}')
        try:
            await self.dev.set_light_state(light_state)
        except SmartDeviceException as ex:
            LOGGER.error(f'{self.pfx} failed: {ex}')
        await self.dev.set_hsv(hue=hue, saturation=bri2st(saturation), value=bri2st(brightness))
        await self.set_state_a()

    def newdev(self):
        return SmartBulb(self.host)

    def cmd_set_on(self, command):
        self.set_on()

    def cmd_set_off(self, command):
        self.set_off()

    def cmd_set_bri(self,command):
        val = int(command.get('value'))
        LOGGER.info(f'{self.pfx} val={val}')
        fut = asyncio.run_coroutine_threadsafe(self.set_bri_a(val), self.controller.mainloop)
        return fut.result()

    def cmd_brt(self,command):
        LOGGER.debug('{self.pfx} connected={self.connected}')
        if not self.dev.is_dimmable:
            LOGGER.error('{self.pfx} Not supported on this device')
        fut = asyncio.run_coroutine_threadsafe(self.brt_a(), self.controller.mainloop)
        return fut.result()

    def cmd_dim(self,command):
        LOGGER.debug('{self.pfx} connected={self.connected}')
        if not self.dev.is_dimmable:
            LOGGER.error('{self.pfx} Not supported on this device')
        fut = asyncio.run_coroutine_threadsafe(self.dim_a(), self.controller.mainloop)
        return fut.result()

    def cmd_set_sat(self,command):
        val = int(command.get('value'))
        LOGGER.info(f'{self.pfx} val={val}')
        fut = asyncio.run_coroutine_threadsafe(self.set_sat_a(val), self.controller.mainloop)
        return fut.result()

    def cmd_set_hue(self,command):
        val = int(command.get('value'))
        LOGGER.info(f'{self.pfx} val={val}')
        fut = asyncio.run_coroutine_threadsafe(self.set_hue_a(val), self.controller.mainloop)
        return fut.result()

    def cmd_set_color_temp(self,command):
        val = int(command.get('value'))
        LOGGER.info(f'val={val}')
        fut = asyncio.run_coroutine_threadsafe(self.set_color_temp_a(val), self.controller.mainloop)
        return fut.result()

    def cmd_set_color_name(self,command):
        val = int(command.get('value'))
        LOGGER.info(f'val={val}')
        fut = asyncio.run_coroutine_threadsafe(self.set_color_name_a(val), self.controller.mainloop)
        return fut.result()

    def cmd_set_color_temp_brightness(self, command):
        query = command.get('query')
        LOGGER.info(f'query={query}')
        fut = asyncio.run_coroutine_threadsafe(
            self.set_color_temp_brightness_a(
                int(query.get('K.uom26')),
                int(query.get('BR.uom100'))
            ), 
            self.controller.mainloop
        )
        return fut.result()

    def cmd_set_color_rgb(self, command):
        query = command.get('query')
        LOGGER.info(f'query={query}')
        fut = asyncio.run_coroutine_threadsafe(
            self.set_color_rgb_a(
                int(query.get('R.uom100')),
                int(query.get('G.uom100')),
                int(query.get('B.uom100')),
                int(query.get('BR.uom100')),
                int(query.get('D.uom42')),
            ),
            self.controller.mainloop
        )
        return fut.result()

    def cmd_set_color_hsv(self, command):
        query = command.get('query')
        LOGGER.info(f'query={query}')
        fut = asyncio.run_coroutine_threadsafe(
            self.set_color_hsv_a(
                int(query.get('H.uom100')),
                int(query.get('S.uom100')),
                int(query.get('BR.uom100')),
                int(query.get('D.uom42')),
            ),
            self.controller.mainloop
        )
        return fut.result()

    def cmd_set_color_xy(self, command):
       LOGGER.error('TODO: Not yet implemented')

    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
        'BRT': cmd_brt,
        'DIM': cmd_dim,
        'SET_BRI': cmd_set_bri,
        'SET_HUE': cmd_set_hue,
        'SET_SAT': cmd_set_sat,
        'CLITEMP' : cmd_set_color_temp,
        'SET_CTBR' : cmd_set_color_temp_brightness,
        'SET_COLOR' : cmd_set_color_name,
        'SET_HSV' : cmd_set_color_hsv,
        'SET_COLOR_RGB': cmd_set_color_rgb,
        'SET_COLOR_XY': cmd_set_color_xy,
    }
