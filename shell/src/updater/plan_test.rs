use super::*;

fn asset(name: &str, tag: &str) -> ReleaseAsset {
    ReleaseAsset {
        name: name.into(),
        url: format!("https://github.com/{GITHUB_REPO}/releases/download/{tag}/{name}"),
    }
}

fn release(version: &str, names: &[&str]) -> Release {
    let tag = format!("{RELEASE_TAG_PREFIX}{version}");
    let mut assets: Vec<_> = names.iter().map(|n| asset(n, &tag)).collect();
    assets.push(asset(SUMS_NAME, &tag));
    Release {
        version: version.into(),
        html_url: format!("https://github.com/{GITHUB_REPO}/releases/tag/{tag}"),
        assets,
    }
}

#[test]
fn ignores_non_app_tags_and_prerelease_names() {
    let body = r#"[
            {"tag_name":"v0.2.0","html_url":"https://github.com/ThatSimpleTech/TST-Desk/releases/tag/v0.2.0","draft":false,"prerelease":false,"assets":[]},
            {"tag_name":"tstdesk-v0.1.0-rc1","html_url":"https://github.com/ThatSimpleTech/TST-Desk/releases/tag/tstdesk-v0.1.0-rc1","draft":false,"prerelease":false,"assets":[]},
            {"tag_name":"tstdesk-v0.1.0","html_url":"https://github.com/ThatSimpleTech/TST-Desk/releases/tag/tstdesk-v0.1.0","draft":false,"prerelease":true,"assets":[]},
            {"tag_name":"tstdesk-v0.1.0","html_url":"https://github.com/ThatSimpleTech/TST-Desk/releases/tag/tstdesk-v0.1.0","draft":false,"prerelease":false,"assets":[]}
        ]"#;
    let parsed = parse_releases(body).unwrap();
    assert_eq!(parsed.len(), 1);
    assert_eq!(parsed[0].version, "0.1.0");
}

#[test]
fn newest_is_the_highest_version() {
    let releases = vec![
        release("0.1.0", &[]),
        release("0.1.10", &[]),
        release("0.2.0", &[]),
    ];
    // 0.1.10 has no assets that matter; newest is by version.
    assert_eq!(newest(&releases).unwrap().version, "0.2.0");
    assert_eq!(compare_versions("0.1.0", "0.2.0"), Ordering::Less);
    assert_eq!(compare_versions("1.0.0", "0.9.9"), Ordering::Greater);
    assert_eq!(compare_versions("0.1.0", "0.1.0"), Ordering::Equal);
}

#[test]
fn picks_the_host_asset_and_refuses_a_foreign_url() {
    let mac = Platform {
        os: "macos",
        arch: "aarch64",
        appimage: false,
    };
    let rel = release(
        "0.2.0",
        &["TST.Desk_0.2.0_aarch64.dmg", "TST.Desk_0.2.0_x64.dmg"],
    );
    let chosen = choose_asset(&rel, &mac).unwrap();
    assert_eq!(chosen.kind, PackageKind::MacosDmg);
    assert!(chosen.name.ends_with("aarch64.dmg"));

    let mut evil = rel.clone();
    evil.assets[0].url = "https://evil.example/TST.Desk_0.2.0_aarch64.dmg".into();
    assert!(choose_asset(&evil, &mac).is_none());

    let linux = Platform {
        os: "linux",
        arch: "x86_64",
        appimage: false,
    };
    let lin = release(
        "0.2.0",
        &["TST.Desk_0.2.0_amd64.deb", "TST.Desk_0.2.0_amd64.AppImage"],
    );
    assert_eq!(choose_asset(&lin, &linux).unwrap().kind, PackageKind::Deb);
    let image = Platform {
        os: "linux",
        arch: "x86_64",
        appimage: true,
    };
    assert_eq!(
        choose_asset(&lin, &image).unwrap().kind,
        PackageKind::AppImage
    );
}

#[test]
fn sums_parser_accepts_both_sha256sum_layouts() {
    let text = "\
abc  ignored
ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  TST.Desk_0.2.0_aarch64.dmg
0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef *SHA256SUMS.txt
";
    let map = parse_sha256sums(text);
    assert_eq!(map.len(), 2);
    assert!(map.contains_key("TST.Desk_0.2.0_aarch64.dmg"));
    assert!(map.contains_key("SHA256SUMS.txt"));
    assert_eq!(
        sha256_hex(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}

#[test]
fn download_urls_stay_on_this_repo() {
    assert!(releases_api_allowed(&releases_url()));
    assert!(initial_download_allowed(
            "https://github.com/ThatSimpleTech/TST-Desk/releases/download/tstdesk-v0.2.0/TST.Desk_0.2.0_aarch64.dmg"
        ));
    assert!(!initial_download_allowed(
            "https://github.com/ThatSimpleTech/TST-Desk/releases/download/v0.2.0/TST.Desk_0.2.0_aarch64.dmg"
        ));
    assert!(!initial_download_allowed(
        "https://github.com/other/TST-Desk/releases/download/tstdesk-v0.2.0/ok.dmg"
    ));
    assert!(!initial_download_allowed(
        "https://github.com/ThatSimpleTech/TST-Desk/releases/download/tstdesk-v0.2.0/../secret"
    ));
    assert!(redirect_host_allowed("objects.githubusercontent.com"));
    assert!(!redirect_host_allowed("example.com"));
    assert!(!redirect_host_allowed(
        "release-assets.githubusercontent.com.example.com"
    ));
    assert!(release_page_allowed(
        "https://github.com/ThatSimpleTech/TST-Desk/releases/tag/tstdesk-v0.2.0"
    ));
    assert!(!release_page_allowed(
        "https://example.com/releases/tag/tstdesk-v0.2.0"
    ));
}

#[test]
fn automatic_is_on_unless_the_file_says_false() {
    assert!(automatic_from_yaml(""));
    assert!(automatic_from_yaml("automatic: true\n"));
    assert!(!automatic_from_yaml("automatic: false\n"));
    assert!(automatic_from_yaml("other: false\n"));
    let dir = std::env::temp_dir().join(format!("tst-desk-update-pref-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    assert!(load_automatic(&dir));
    store_automatic(&dir, false).unwrap();
    assert!(!load_automatic(&dir));
    store_automatic(&dir, true).unwrap();
    assert!(load_automatic(&dir));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn github_asset_store_redirect_is_followed() {
    // TD-5204: GitHub's 302 for a release download lands here today; with it
    // missing, every update download failed with "error following redirect".
    let store = |url: &str| redirect_allowed(&reqwest::Url::parse(url).unwrap());
    assert!(store(
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc?sp=r"
    ));
    assert!(store(
        "https://objects.githubusercontent.com/github-production-release-asset-2e65be/1"
    ));
    assert!(!store(
        "http://release-assets.githubusercontent.com/github-production-release-asset/1"
    ));
    assert!(!store(
        "https://release-assets.githubusercontent.com:8443/asset"
    ));
    assert!(!store("https://example.com/asset"));
}
