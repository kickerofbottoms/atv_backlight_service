# atv_backlight_service

## What
Minimal service to sync an Apple TVs power state with a Hue backlight.

Lives on a computer on the network, watches for changes in Apple TV power states, commands
a single Hue light accordingly.

## Why

Because Homekit doesn't have a power state-based trigger for automations, for some dumb reason.
And even if you're ok using a Hue account to link your junk with Homekit (which itself is dumb because there's no part
of that stack that actually needs the cloud for anything), then you gotta run a full
node.js Homebridge instance somewhere to use the Appple TV Enhanced plugin, which on top of the node.js is calling into
Python, which for my very specific use case of only linking a light behind the TV to the TV, seemed like
a massive overkill of a stack to have running on my pi all the time. (If someone knows of a simpler existing solution,
I'm all ears.)

So this is the relevant pieces of that setup jankily jammed together without all the extra stuff I didn't need.

## Setup
1. Install this somewhere, install `requirements.txt`
2. Pair one or more Apple TVs using the wizard:
   
   `python3 -m atv_backlight_service atv pair`
3. Pair the Hue bridge using the wizard:
    
   `python3 -m atv_backlight_service hue pair <bridge_address>`
4. Attach a backlight to each Apple TV using the wizard:

    `python3 -m atv_backlight_service attach`
5. Run the service

    `python3 -m atv_backlight_service run`
6. Probably make that last command into a systemd service, or run via
    Windows Task Manager / startup scripts, or whatever. See `atv_backlight.service`

## Credits

This is basically just:
- https://github.com/postlund/pyatv (90%)
- https://github.com/studioimaginaire/phue (5%)
- https://github.com/kickerofbottoms/atv_backlight_service (5%)

So, a proportional amount of thanks to each of those authors.
