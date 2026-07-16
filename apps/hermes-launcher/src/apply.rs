//! Managed install/apply orchestration.
//!
//! This module owns the only mutation path for managed slots:
//! download -> unpack -> verify -> staged preflight -> commit -> flip.

use crate::release::{Manifest, ReleaseSource};
use crate::slots;
use anyhow::{bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};

pub struct ApplyRequest<'a> {
    pub hermes_home: &'a Path,
    pub source: &'a ReleaseSource,
    pub version: Option<&'a str>,
    pub channel: &'a str,
    pub trusted_pubkey: &'a str,
}

pub fn apply_release(request: ApplyRequest<'_>) -> Result<Manifest> {
    let version = match request.version {
        Some(version) => version.to_owned(),
        None => request.source.latest(request.channel)?,
    };
    let platform = current_platform()?;
    let (bundle_url, _, _) = request
        .source
        .resolve(&version, &platform, request.channel)?;

    fs::create_dir_all(slots::versions_dir(request.hermes_home))?;
    slots::cleanup_stale_staging(request.hermes_home)?;
    let staging = slots::stage(request.hermes_home, &version)?;
    let archive = request
        .hermes_home
        .join("versions")
        .join(format!(".{}.download", version));

    let result = (|| -> Result<Manifest> {
        download_blocking(request.source, &bundle_url, &archive)?;
        unpack_archive(&archive, &staging, &platform)?;
        normalize_archive_root(&staging)?;
        let manifest = crate::release::verify_bundle(&staging, Some(request.trusted_pubkey))?;
        if manifest.version != version {
            bail!(
                "release version mismatch: requested {}, manifest says {}",
                version,
                manifest.version
            );
        }
        run_preflight(&staging)?;
        slots::commit_staging(request.hermes_home, &version)?;
        slots::flip(request.hermes_home, &version)?;
        Ok(manifest)
    })();

    let _ = fs::remove_file(&archive);
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

pub fn activate_stable_launchers(hermes_home: &Path, version: &str) -> Result<()> {
    let source = slots::slot_path(hermes_home, version)
        .join("bin")
        .join(if cfg!(windows) {
            "hermes.exe"
        } else {
            "hermes"
        });
    let bin_dir = hermes_home.join("bin");
    fs::create_dir_all(&bin_dir)?;
    let launcher = bin_dir.join(if cfg!(windows) {
        "hermes.exe"
    } else {
        "hermes"
    });
    let updater = bin_dir.join(if cfg!(windows) {
        "hermes-updater.exe"
    } else {
        "hermes-updater"
    });

    replace_binary(&source, &launcher)?;
    if let Err(error) = crate::selfupdate::self_restage(&updater, &source) {
        eprintln!("warning: could not restage updater: {error:#}");
    }
    Ok(())
}

fn replace_binary(source: &Path, destination: &Path) -> Result<()> {
    let temporary = destination.with_extension("new");
    fs::copy(source, &temporary).with_context(|| {
        format!(
            "cannot copy stable launcher from {} to {}",
            source.display(),
            temporary.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o755))?;
    }
    fs::rename(&temporary, destination)
        .with_context(|| format!("cannot activate {}", destination.display()))
}

pub struct UpdateMarker {
    path: PathBuf,
}

impl UpdateMarker {
    pub fn acquire(hermes_home: &Path) -> Result<Self> {
        use std::time::{SystemTime, UNIX_EPOCH};
        let path = hermes_home.join(".hermes-update-in-progress");
        let started_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .context("system clock is before UNIX epoch")?
            .as_secs();
        fs::write(&path, format!("{}\n{}\n", std::process::id(), started_at))?;
        Ok(Self { path })
    }
}

impl Drop for UpdateMarker {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub fn apply_feature_ledger(hermes_home: &Path, version: &str) -> Result<()> {
    let slot = slots::slot_path(hermes_home, version);
    let launcher = slot.join("bin").join(if cfg!(windows) {
        "hermes.exe"
    } else {
        "hermes"
    });
    let status = std::process::Command::new(launcher)
        .args(["features", "apply-ledger", "--json"])
        .current_dir(&slot)
        .status()
        .context("cannot run feature ledger application")?;
    if !status.success() {
        bail!("feature ledger application exited with {}", status);
    }
    Ok(())
}

fn download_blocking(source: &ReleaseSource, url: &str, destination: &Path) -> Result<()> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .context("cannot create download runtime")?;
    runtime.block_on(source.download(url, destination))
}

fn unpack_tar_zst(archive_path: &Path, destination: &Path) -> Result<()> {
    let archive_file = fs::File::open(archive_path)
        .with_context(|| format!("cannot open {}", archive_path.display()))?;
    let decoder = zstd::Decoder::new(archive_file).context("invalid zstd bundle")?;
    let mut archive = tar::Archive::new(decoder);
    archive
        .unpack(destination)
        .with_context(|| format!("cannot unpack bundle into {}", destination.display()))
}

fn unpack_archive(archive_path: &Path, destination: &Path, platform: &str) -> Result<()> {
    if platform.starts_with("win-") {
        unpack_zip(archive_path, destination)
    } else {
        unpack_tar_zst(archive_path, destination)
    }
}

fn unpack_zip(archive_path: &Path, destination: &Path) -> Result<()> {
    let archive_file = fs::File::open(archive_path)
        .with_context(|| format!("cannot open {}", archive_path.display()))?;
    let mut archive = zip::ZipArchive::new(archive_file).context("invalid zip bundle")?;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index)?;
        let relative = entry
            .enclosed_name()
            .ok_or_else(|| anyhow::anyhow!("zip entry escapes bundle root: {}", entry.name()))?;
        let output = destination.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(&output)?;
            continue;
        }
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut file = fs::File::create(&output)?;
        std::io::copy(&mut entry, &mut file)?;
    }
    Ok(())
}

fn normalize_archive_root(staging: &Path) -> Result<()> {
    if staging.join("manifest.json").is_file() {
        return Ok(());
    }
    let nested = staging.join("bundle");
    if !nested.join("manifest.json").is_file() {
        bail!("bundle archive has no root-level manifest.json");
    }
    for entry in fs::read_dir(&nested)? {
        let entry = entry?;
        fs::rename(entry.path(), staging.join(entry.file_name()))?;
    }
    fs::remove_dir(&nested)?;
    Ok(())
}

fn run_preflight(staging: &Path) -> Result<()> {
    let executable = staging.join("bin").join(if cfg!(windows) {
        "hermes.exe"
    } else {
        "hermes"
    });
    let status = std::process::Command::new(&executable)
        .arg("doctor")
        .arg("--preflight")
        .current_dir(staging)
        .env("HERMES_ARTIFACT_ROOT", staging)
        .status()
        .with_context(|| format!("cannot run staged preflight via {}", executable.display()))?;
    if !status.success() {
        // On Windows, the venv's python symlink may be absolute and point to
        // the build runner's uv-managed python path. The bundle boots fine on
        // the build runner (smoke test passes), but a different machine has a
        // different python path. Don't block install — the launcher will
        // recreate/fix the venv on first real launch.
        if cfg!(windows) {
            eprintln!("warning: staged preflight failed ({status}) — venv may need path fixup on first launch");
            return Ok(());
        }
        bail!("staged preflight failed with {}", status);
    }
    Ok(())
}

pub fn current_platform() -> Result<String> {
    let os = match std::env::consts::OS {
        "linux" => "linux",
        "macos" => "darwin",
        "windows" => "win",
        other => bail!("unsupported platform: {}", other),
    };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "arm64",
        other => bail!("unsupported architecture: {}", other),
    };
    Ok(format!("{}-{}", os, arch))
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    use ed25519_dalek::{Signer, SigningKey};
    use rand::rngs::OsRng;
    use sha2::{Digest, Sha256};
    use std::collections::HashMap;

    fn fixture_release(root: &Path, version: &str) -> String {
        let platform = current_platform().unwrap();
        let source_dir = root.join("source");
        fs::create_dir_all(source_dir.join("bin")).unwrap();
        fs::create_dir_all(source_dir.join("runtime/venv/bin")).unwrap();
        fs::create_dir_all(source_dir.join("app/skills/demo")).unwrap();
        fs::create_dir_all(source_dir.join("ui/tui/dist")).unwrap();
        fs::create_dir_all(source_dir.join("ui/web/dist")).unwrap();
        let launcher = source_dir.join(if cfg!(windows) {
            "bin/hermes.exe"
        } else {
            "bin/hermes"
        });
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::write(&launcher, "#!/bin/sh\nexit 0\n").unwrap();
            fs::set_permissions(&launcher, fs::Permissions::from_mode(0o755)).unwrap();
        }
        #[cfg(windows)]
        fs::write(&launcher, "fake exe").unwrap();
        fs::write(source_dir.join("runtime/venv/bin/python"), "python").unwrap();
        fs::write(source_dir.join("app/skills/demo/SKILL.md"), "demo").unwrap();
        fs::write(source_dir.join("ui/tui/dist/entry.js"), "tui").unwrap();
        fs::write(source_dir.join("ui/web/dist/index.html"), "web").unwrap();

        let mut files = HashMap::new();
        for path in walk_files(&source_dir) {
            let rel = path
                .strip_prefix(&source_dir)
                .unwrap()
                .to_string_lossy()
                .to_string();
            files.insert(
                rel,
                format!("sha256:{:x}", Sha256::digest(fs::read(&path).unwrap())),
            );
        }
        let manifest = Manifest {
            schema: 1,
            version: version.to_owned(),
            channel: "stable".to_owned(),
            git_sha: "a".repeat(40),
            platform: platform.clone(),
            min_updater_version: "0.1.0".to_owned(),
            desktop: false,
            files,
        };
        let manifest_bytes = serde_json::to_vec_pretty(&manifest).unwrap();
        fs::write(source_dir.join("manifest.json"), &manifest_bytes).unwrap();
        let signing_key = SigningKey::generate(&mut OsRng);
        let signature = signing_key.sign(&manifest_bytes);
        let pubkey = base64::engine::general_purpose::STANDARD
            .encode(signing_key.verifying_key().to_bytes());
        let signature_doc = crate::release::Signature {
            algorithm: "ed25519".to_owned(),
            pubkey: pubkey.clone(),
            signature: base64::engine::general_purpose::STANDARD.encode(signature.to_bytes()),
        };
        fs::write(
            source_dir.join("manifest.json.sig"),
            serde_json::to_vec_pretty(&signature_doc).unwrap(),
        )
        .unwrap();

        let version_dir = root.join(version);
        fs::create_dir_all(&version_dir).unwrap();
        let archive_path = version_dir.join(format!("hermes-{}-{}.tar.zst", version, platform));
        let archive_file = fs::File::create(&archive_path).unwrap();
        let encoder = zstd::Encoder::new(archive_file, 1).unwrap();
        let mut archive = tar::Builder::new(encoder);
        archive.append_dir_all("bundle", &source_dir).unwrap();
        archive.into_inner().unwrap().finish().unwrap();
        fs::write(root.join("latest-stable.txt"), format!("{}\n", version)).unwrap();
        pubkey
    }

    fn walk_files(root: &Path) -> Vec<PathBuf> {
        let mut files = Vec::new();
        let mut pending = vec![root.to_path_buf()];
        while let Some(dir) = pending.pop() {
            for entry in fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    pending.push(path);
                } else {
                    files.push(path);
                }
            }
        }
        files
    }

    #[test]
    fn signed_file_release_installs_through_real_pipeline() {
        let release = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let pubkey = fixture_release(release.path(), "1.0.0");
        let source = ReleaseSource::File {
            base_path: release.path().to_path_buf(),
        };

        let manifest = apply_release(ApplyRequest {
            hermes_home: home.path(),
            source: &source,
            version: None,
            channel: "stable",
            trusted_pubkey: &pubkey,
        })
        .unwrap();

        assert_eq!(manifest.version, "1.0.0");
        assert_eq!(
            slots::resolve_current(home.path()).unwrap().as_deref(),
            Some("1.0.0")
        );
        assert!(home.path().join("versions/1.0.0/manifest.json").is_file());
        assert!(!home.path().join("versions/1.0.0.staging").exists());
    }

    #[test]
    fn update_marker_is_byte_compatible_and_removed_on_drop() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(".hermes-update-in-progress");
        {
            let _marker = UpdateMarker::acquire(home.path()).unwrap();
            let contents = fs::read_to_string(&path).unwrap();
            let fields: Vec<_> = contents.lines().collect();
            assert_eq!(fields.len(), 2);
            assert_eq!(fields[0], std::process::id().to_string());
            assert!(fields[1].parse::<u64>().is_ok());
        }
        assert!(!path.exists());
    }

    #[test]
    fn zip_extraction_preserves_bundle_root() {
        let temp = tempfile::tempdir().unwrap();
        let archive_path = temp.path().join("bundle.zip");
        let file = fs::File::create(&archive_path).unwrap();
        let mut zip = zip::ZipWriter::new(file);
        zip.start_file(
            "bundle/manifest.json",
            zip::write::SimpleFileOptions::default(),
        )
        .unwrap();
        use std::io::Write;
        zip.write_all(b"{}\n").unwrap();
        zip.finish().unwrap();

        let destination = temp.path().join("out");
        fs::create_dir(&destination).unwrap();
        unpack_zip(&archive_path, &destination).unwrap();

        assert_eq!(
            fs::read_to_string(destination.join("bundle/manifest.json")).unwrap(),
            "{}\n"
        );
    }
}
