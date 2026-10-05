#!/usr/bin/env python3
"""Build and verify selected vanilla Apple products, then publish one private release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

LABELS = {"tvos": "tvOS", "ios": "iOS"}
OUTPUT_TAG = "celeste-tvos-output"  # Preserve existing private builders and Cleanup.
LIMIT = 2 * 1024**3


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def platforms(choice: str) -> tuple[str, ...]:
    require(choice in (*LABELS, "both"), "choose tvos, ios, or both")
    return ("tvos", "ios") if choice == "both" else (choice,)


def names(platform: str) -> tuple[str, str]:
    return (f"Celeste-{LABELS[platform]}-unsigned.ipa", f"Celeste-{LABELS[platform]}-build.txt")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(arguments, **kwargs):
    return subprocess.run([str(value) for value in arguments], check=True, **kwargs)


def inspect_ipa(ipa: Path, platform: str, bundle_id: str) -> str:
    require(ipa.is_file() and not ipa.is_symlink() and 0 < ipa.stat().st_size < LIMIT,
            "IPA is missing, linked, empty, or at/above the 2 GiB Release limit")
    with zipfile.ZipFile(ipa) as archive:
        entries = archive.infolist()
        seen = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            require(not path.is_absolute() and ".." not in path.parts and "\\" not in entry.filename,
                    "unsafe IPA entry")
            require(entry.filename.casefold() not in seen, "duplicate IPA entry")
            seen.add(entry.filename.casefold())
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(mode in (0, stat.S_IFREG, stat.S_IFDIR), "linked or special IPA entry")
            require("_CodeSignature" not in path.parts and path.name != "embedded.mobileprovision",
                    "cloud products must be unsigned")
        plists = [entry.filename for entry in entries
                  if len(PurePosixPath(entry.filename).parts) == 3
                  and entry.filename.startswith("Payload/")
                  and entry.filename.endswith(".app/Info.plist")]
        require(len(plists) == 1, "IPA must contain exactly one main app")
        info = plistlib.loads(archive.read(plists[0]))
        expected = "iPhoneOS" if platform == "ios" else "AppleTVOS"
        require(info.get("CFBundleSupportedPlatforms") == [expected], "IPA platform differs from request")
        require(info.get("UIDeviceFamily") == ([1, 2] if platform == "ios" else [3]),
                "IPA device families differ from request")
        require(info.get("CFBundleIdentifier") == bundle_id, "IPA bundle identifier differs from request")
        return str(PurePosixPath(plists[0]).parent)


def build(args) -> None:
    selected = platforms(args.platform)
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is not None, "invalid source commit")
    identities = {"tvos": args.bundle_id, "ios": args.ios_bundle_id}
    for platform in selected:
        require(re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", identities[platform]) is not None,
                f"invalid {platform} bundle identifier")
    source = args.source_root.resolve()
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=False)
    products = []
    for platform in selected:  # Deliberately serial: app AOT must never overlap.
        print(f"Building and verifying vanilla {LABELS[platform]}", flush=True)
        command = [source / f"build-{platform}.sh", "--non-interactive",
                   "--game-root", args.game_root, "--fmod-root", args.fmod_root,
                   "--bundle-id", identities[platform], "--no-color"]
        command += ["--unsigned"] if platform == "ios" else ["--mode", "ipa"]
        run(command, cwd=source)
        if platform == "ios":
            manifest = json.loads((source / "artifacts/ios/build-manifest.json").read_text())
            expected = {"product": "Celeste iOS", "signing": "unsigned", "rid": "ios-arm64",
                        "configuration": "Release", "fullAOT": True, "fullTrim": True,
                        "useInterpreter": False, "jit": False}
            require(all(type(manifest.get(key)) is type(value) and manifest[key] == value
                        for key, value in expected.items()), "iOS build manifest violates product policy")
            filename = manifest.get("ipaName", "")
            require(filename == Path(filename).name and filename.endswith("-unsigned.ipa"),
                    "iOS manifest has an invalid IPA filename")
            ipa = source / "artifacts/ios" / filename
            app = inspect_ipa(ipa, platform, identities[platform])
            require(ipa.stat().st_size == manifest["ipaBytes"] and sha256(ipa) == manifest["ipaSha256"],
                    "iOS IPA differs from its build manifest")
            # Verify the actual uploaded IPA's app, not a neighbouring build directory.
            with tempfile.TemporaryDirectory(prefix="cloud-verify-", dir=source / ".build/ios-self-build") as tmp:
                with zipfile.ZipFile(ipa) as archive:
                    archive.extractall(tmp)  # All entries passed inspect_ipa above.
                run(["python3", source / "scripts/verify-ios-package.py", "--app", Path(tmp) / app,
                     "--lane", "device", "--product", "celeste", "--signing", "unsigned"], cwd=source)
            profile = manifest["profileId"]
        else:
            ipa = source / "dist/Celeste-tvOS-unsigned.ipa"
            inspect_ipa(ipa, platform, identities[platform])
            run([source / "scripts/verify-celeste-tvos-stage14.sh", "--ipa", ipa,
                 "--output", "artifacts/tvos-self-build/cloud-stage14-verification.json"], cwd=source)
            generated = ".build/celeste-runtime/stage6-current/audio/managed"
            run([source / "scripts/verify-celeste-tvos-stage15.sh", "--generated-root", generated,
                 "--ipa", ipa], cwd=source)
            run([source / "scripts/verify-celeste-tvos-stage16b.sh", "--generated-root", generated,
                 "--native-manifest", ".build/tvos-host/normalized-manifest.json", "--ipa", ipa], cwd=source)
            profile = json.loads((source / ".build/celeste-runtime/stage18c-cloud/celeste-input.json").read_text())["profileId"]
        ipa_name, metadata_name = names(platform)
        shutil.copyfile(ipa, output / ipa_name)
        digest = sha256(output / ipa_name)
        metadata = (f"Celeste {LABELS[platform]} private cloud build\nPublic source commit: {args.source_sha}\n"
                    f"Celeste profile: {profile}\nIPA bytes: {ipa.stat().st_size}\nIPA SHA-256: {digest}\n"
                    f"Configuration: Release {platform}-arm64\nFull AOT: true\nFull trimming: true\n"
                    "UseInterpreter: false\nSigned: false\n")
        (output / metadata_name).write_text(metadata)
        for name in (ipa_name, metadata_name):
            path = output / name
            products.append({"name": name, "size": path.stat().st_size, "sha256": sha256(path)})
    # Written only after every selected builder and verifier has succeeded.
    (output / "products.json").write_text(json.dumps(products, indent=2) + "\n")


def verified_products(output: Path, choice: str) -> list[dict]:
    records = json.loads((output / "products.json").read_text())
    expected = {name for platform in platforms(choice) for name in names(platform)}
    require(len(records) == len(expected) and {item["name"] for item in records} == expected,
            "staged products do not match the requested platforms")
    for item in records:
        path = output / item["name"]
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size < LIMIT
                and path.stat().st_size == item["size"] and sha256(path) == item["sha256"],
                "staged product changed after verification")
    return records


def verify_release(release: dict, records: list[dict], draft: bool) -> None:
    assets = release.get("assets", [])
    require(release.get("isDraft") is draft and release.get("isPrerelease") is False,
            "unexpected output Release state")
    require(len(assets) == len(records) and {item["name"] for item in assets} == {item["name"] for item in records},
            "private output Release has missing, duplicate, or extra assets")
    for record in records:
        asset = next(item for item in assets if item["name"] == record["name"])
        require(asset.get("size") == record["size"], "GitHub output size differs")
        require(not asset.get("digest") or asset["digest"] == "sha256:" + record["sha256"],
                "GitHub output digest differs")


def publish(args) -> None:
    output = args.output_root.resolve()
    records = verified_products(output, args.platform)
    repository = os.environ["GITHUB_REPOSITORY"]
    # Repeat privacy/overwrite checks immediately before the only remote mutation.
    run([Path(__file__).with_name("cloud-common.sh"), "require-private"])
    run([Path(__file__).with_name("cloud-common.sh"), "ensure-output-absent"])
    notes = output / "release-notes.md"
    notes.write_text("# Celeste — unsigned Apple builds\n\n"
                     "These verified full-AOT vanilla IPAs require separate signing and installation.\n"
                     "Keep them private. Download them, then run **Clean private build files**.\n\n" +
                     "\n".join("```text\n" + (output / names(p)[1]).read_text() + "```\n"
                               for p in platforms(args.platform)))
    run(["gh", "release", "create", OUTPUT_TAG, "--repo", repository, "--target", os.environ["GITHUB_SHA"],
         "--draft", "--title", "Celeste — unsigned Apple builds", "--notes-file", notes,
         *[output / item["name"] for item in records]])
    def check(draft):
        response = run(["gh", "release", "view", OUTPUT_TAG, "--repo", repository,
                        "--json", "isDraft,isPrerelease,assets"], capture_output=True, text=True)
        verify_release(json.loads(response.stdout), records, draft)
    check(True)
    run(["gh", "release", "edit", OUTPUT_TAG, "--repo", repository, "--draft=false"])
    check(False)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("# Celeste — Build complete\n\n"
                      f"[Download unsigned IPAs](https://github.com/{repository}/releases/tag/{OUTPUT_TAG})\n\n" +
                      "\n".join(f"- `{item['name']}`" for item in records) +
                      "\n\nFull AOT, full trimming, interpreter disabled; each actual IPA verified.\n\n"
                      "Download, run **Clean private build files**, then sign and install for your device.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    builder = sub.add_parser("build")
    publisher = sub.add_parser("publish")
    for command in (builder, publisher):
        command.add_argument("--platform", choices=(*LABELS, "both"), required=True)
        command.add_argument("--output-root", type=Path, required=True)
    for name in ("source-root", "game-root", "fmod-root"):
        builder.add_argument("--" + name, type=Path, required=True)
    for name in ("source-sha", "bundle-id", "ios-bundle-id"):
        builder.add_argument("--" + name, required=True)
    args = parser.parse_args()
    (build if args.command == "build" else publish)(args)


if __name__ == "__main__":
    main()
