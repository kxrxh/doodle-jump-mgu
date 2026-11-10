# Doodle Jump for MGU7 WIP

Original Doodle Jump 3.10.0 for BMW iDrive 7. Touch, joystick and rotary ring controls.

About 66 MB to download, about 190 MB after first launch.

## Install

In mgu-store, add this GitHub source, select **Releases**, and install **Doodle Jump**:

https://github.com/kxrxh/doodle-jump-mgu

The ready package includes the game files and runtime. Installation downloads and verifies the package without building the game or downloading an APK on the head unit. BMW’s SDK comes from the head unit. The `main` branch is for source builds.

You can also add the catalog URL directly:

https://raw.githubusercontent.com/kxrxh/doodle-jump-mgu/main/catalog.json

The app uses mgu-store's renderer without installing a controller-gate tool. An existing HMI controller gate is reused through the renderer. Without one, touch controls remain available; the HMI retains the iDrive controller, so exclusive ring and joystick control is not guaranteed.

## Local

Run `./run-local.sh` with Python 3.13, Node.js and a C compiler. Open http://localhost:8079.

Tested in the original MGU emulator. Physical head-unit installation and audio remain unverified.

## Screenshots

Menu

![Menu](docs/screenshots/menu.png)

Gameplay

![Gameplay](docs/screenshots/gameplay.png)
