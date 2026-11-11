"""Build the offline 2048 APK with an installed JDK and Android SDK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile


def build(jdk, sdk, output, keystore):
    jdk, sdk = Path(jdk).resolve(strict=True), Path(sdk).resolve(strict=True)
    output, keystore = Path(output).resolve(), Path(keystore).resolve()
    source = Path(__file__).resolve().parents[1] / "games" / "android-2048"
    tools = sdk / "build-tools" / "35.0.0"
    android = sdk / "platforms" / "android-35" / "android.jar"
    suffix = ".exe" if os.name == "nt" else ""
    java, javac, keytool = [jdk / "bin" / (name + suffix) for name in ("java", "javac", "keytool")]
    aapt, zipalign = [tools / (name + suffix) for name in ("aapt2", "zipalign")]
    d8, signer = [tools / "lib" / (name + ".jar") for name in ("d8", "apksigner")]
    for dependency in (java, javac, keytool, aapt, zipalign, d8, signer, android):
        if not dependency.is_file():
            raise FileNotFoundError(dependency)
    output.mkdir(parents=True, exist_ok=False)
    classes, dex = output / "classes", output / "dex"
    classes.mkdir(); dex.mkdir()

    def run(*argv, **kwargs):
        return subprocess.run([str(arg) for arg in argv], cwd=output, timeout=120, check=True, **kwargs)

    run(javac, "--release", "8", "-encoding", "UTF-8", "-classpath", android,
        "-d", classes, source / "GameActivity.java")
    run(java, "-cp", d8, "com.android.tools.r8.D8", "--min-api", "23", "--lib", android,
        "--output", dex, *sorted(classes.rglob("*.class")))
    resources, unsigned, aligned, signed = [output / name for name in
        ("resources.apk", "unsigned.apk", "aligned.apk", "lanya-2048.apk")]
    run(aapt, "link", "-I", android, "--manifest", source / "AndroidManifest.xml", "-o", resources)
    with zipfile.ZipFile(resources) as base, zipfile.ZipFile(unsigned, "x", zipfile.ZIP_DEFLATED) as apk:
        for entry in base.infolist():
            apk.writestr(entry, base.read(entry))
        apk.write(dex / "classes.dex", "classes.dex")
    run(zipalign, "-p", "4", unsigned, aligned)
    # An app-local debug key, not a user's Android account or production key.
    if not keystore.exists():
        run(keytool, "-genkeypair", "-keystore", keystore, "-storepass", "android",
            "-alias", "androiddebugkey", "-keypass", "android", "-dname", "CN=Lanya Local Debug",
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000")
    run(java, "-jar", signer, "sign", "--ks", keystore, "--ks-key-alias", "androiddebugkey",
        "--ks-pass", "pass:android", "--key-pass", "pass:android", "--out", signed, aligned)
    run(java, "-jar", signer, "verify", "--min-sdk-version", "23", "--verbose", signed)
    run(zipalign, "-c", "-p", "4", signed)
    permissions = run(aapt, "dump", "permissions", signed, capture_output=True, text=True).stdout
    if "org.dshlanya.game2048" not in permissions or "uses-permission" in permissions:
        raise ValueError("Unexpected game package or Android permission")
    with signed.open("rb") as stream:
        return {"apk": str(signed), "package": "org.dshlanya.game2048", "permissions": [],
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("jdk", "sdk", "output", "keystore"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.jdk, args.sdk, args.output, args.keystore), ensure_ascii=True))
