// electron-builder afterPack hook: ad-hoc code-sign the packaged .app before it
// gets wrapped into the DMG.
//
// Without any signature, macOS (especially Apple Silicon) reports an unsigned
// downloaded app as "damaged and can't be opened". An ad-hoc signature
// (`codesign --sign -`, no certificate) makes the signature valid so it opens
// after the quarantine flag is cleared, instead of being flagged as damaged.
//
// This is NOT notarization — a browser download is still quarantined, so first
// launch still needs a right-click → Open (or `xattr -dr com.apple.quarantine`).
// Real signing + notarization (a paid Apple Developer cert) is what removes that
// last step; this just fixes the misleading "damaged" wording.

const { execFileSync } = require('child_process');
const path = require('path');

exports.default = async function afterPack(context) {
  const { electronPlatformName, appOutDir, packager } = context;
  if (electronPlatformName !== 'darwin') return;

  const appName = `${packager.appInfo.productFilename}.app`;
  const appPath = path.join(appOutDir, appName);

  // eslint-disable-next-line no-console
  console.log(`[afterPack] ad-hoc signing ${appPath}`);
  execFileSync(
    'codesign',
    ['--force', '--deep', '--sign', '-', '--timestamp=none', appPath],
    { stdio: 'inherit' }
  );
};
