# Companion app (optional)

Android only lets the **current default SMS app** write to the message store. That is a
platform rule with no way around it, so a PC cannot restore your texts on its own. This app
does the writing on the phone side.

You do not need it for photos, videos, files, contacts or apps — those all work from the PC
alone. It matters for two things:

| | Without the app | With the app |
|---|---|---|
| Text messages, restore | not possible | works |
| Call log, restore | not possible | works |
| Contacts/SMS **export** on a locked-down build | blocked by the OEM | works |

That last row is the other reason it exists. Some builds (a lot of Android 14+ OEM ROMs)
refuse to let the `adb shell` user read the SMS and contacts providers. On those phones the
PC-side export fails cleanly and tells you to install this.

## Building

```bash
cd companion
./gradlew assembleRelease
```

The APK lands in `app/build/outputs/apk/release/`. Install it with:

```bash
adb install -r app-release.apk
```

## How the desktop app talks to it

Via explicit intents, so nothing is exposed to other apps:

```
am start -n com.ptransfer.companion/.ExportActivity --es out  /sdcard/ptransfer-export
am start -n com.ptransfer.companion/.ImportActivity --es file /sdcard/Download/ptransfer/sms.json --es kind sms
```

`ptransfer` detects the package automatically and adjusts its instructions.

## Restoring messages

1. Install the app on the **new** phone.
2. Run the restore from the PC. It opens the app.
3. The app asks to become the default SMS app. Accept — Android requires it to write.
4. Tap Import.
5. **Switch your normal messaging app back.** The app reminds you.

It asks for the SMS role only while importing and hands it straight back.

## Status

The Kotlin sources here are a working scaffold: manifest, permissions, SMS-role handling, and
the import/export activities with their JSON and vCard handling. Wire-up against a real device
is the remaining work, which is why the desktop side never depends on it — every feature
degrades to a clear on-screen instruction when the app is absent.
