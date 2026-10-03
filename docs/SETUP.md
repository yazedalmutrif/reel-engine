# Setup: Yazeed's one-time ~15 minutes

Everything else is automatic. You do these steps once, in order. Items marked **VERIFY** are Meta screen labels that can differ slightly; the meaning is what counts.

> The engine is installed in the project folder (by `scripts\setup.ps1`) and runs in **dry-run**: it edits, captions and schedules your clips, but posts nothing until you say so (step 7).

## 1. Create the Instagram account (phone, ~4 min)
1. Instagram app → create a new account. Pick the handle.
2. **Settings → Account type and tools → Switch to professional account → Creator** (Business also works with the API).
3. **Security first:**
   - Settings → Accounts Center → Password and security → **Two-factor authentication → Authentication app** (not SMS). Save the **recovery codes offline** (on paper, or in your password manager).
   - Use a **unique password** from a password manager.
   - Turn on **login alerts**.

## 2. Create the Meta app (computer, ~5 min)
1. Go to <https://developers.facebook.com/apps> → **Create app** → type **Business**.
2. In the app: **Add product → Instagram** → **API setup with Instagram login** (VERIFY label).
3. **App roles → Roles → Instagram Testers → Add** your new account (VERIFY label). Then, in the Instagram app (phone): **Settings → Website permissions / Apps and websites → Tester invites → Accept** (VERIFY label).
4. The app stays in **Development mode**. No App Review or Business Verification is needed for your own account.
5. Permissions needed (least privilege): `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_insights`. Don't add the comments permission.

## 3. Generate the token and connect (computer, ~2 min)
1. In the app dashboard: Instagram → API setup → your account → **Generate token** (it's a 60-day long-lived token). Copy it.
2. In PowerShell:
   ```powershell
   cd <path to>\ReelEngine
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\connect-instagram.ps1
   ```
   Paste the token when asked (the input is hidden). The engine checks it with Instagram and stores it in **Windows Credential Manager** only, never in a file.
3. Check everything: `.venv\Scripts\reel.exe doctor --live`. It prints your account type, the publishing quota and the token expiry. The engine refreshes the token every week by itself.
   - Optional, once: `reel doctor --live --probe-container` settles which API host accepts Reel containers (VERIFY in API_NOTES). It creates one unpublished test container that expires by itself.

## 4. The inbox (phone, ~2 min)
- Install **OneDrive** on the iPhone and sign in with the same account as the PC.
- Film, then upload the clip to the **`Reel Inbox`** folder (OneDrive app → + → Upload, or share from Photos → OneDrive → `Reel Inbox`).
- On the PC: right-click the `Reel Inbox` folder in OneDrive (`[paths].inbox` in config.toml) → **Always keep on this device**, so clips download straight away.
- The engine waits until a clip has finished syncing, then *moves* it out of OneDrive into the project (this saves cloud space) and takes it from there.

## 5. Filming (every week)
- The weekly **filming brief** (vault: `12 Social Media/Filming Briefs/`, and the dashboard's Brief tab) gives you the hook line word for word, 3 beats, the CTA, the length and a shot note.
- Portrait, face in the middle of the frame, facing a window. 1080p or 4K at 30 fps, HDR on or off (the engine handles both). Say the hook first; no «السلام عليكم» before it.
- Mess-ups are fine: pause and say the sentence again. The engine keeps your **last good take** and cuts pauses, fillers and false starts.

## 6. Turn on the automatic schedule (one command; this is your OK)
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -WhatIf   # preview
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1          # register
```
This creates three tasks: *Brain - Reel Engine Tick* (every 15 min), *Daily* (03:30) and *Weekly* (Saturday 20:00). They run while you're signed in (a locked screen is fine). Make sure **Power Options → Allow wake timers** is enabled.

## 7. Go live (only when you're ready)
1. Watch the dry-run for a few days in the dashboard (`reel dashboard --open`): the reels, their captions, the QA results and the schedule.
2. Add this line to `docs/DECISIONS.md` (your recorded OK; in a fresh clone, create that file first: it is the owner's private decision log and isn't in the repo):
   `- 2026-MM-DD LIVE-OK: Yazeed approved going live`
3. Run `reel mode live` and type `GO LIVE` when asked. It refuses if the token or `reel doctor --live` isn't healthy.
4. Reels that were only rehearsed in dry-run can be posted for real with `reel requeue <id>`.

**Stop posting anytime:** `reel pause` (undo with `reel resume`). Posting stops from the next 15-minute tick.

## Monthly habit (2 min)
- Instagram → Settings → **Account Status**: check recommendation eligibility.
- Meta **Accounts Center → Apps and websites**: remove anything you don't recognise.
