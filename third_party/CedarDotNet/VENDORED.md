Vendored from https://github.com/jamiewinder/CedarDotNet at 84e3e01 (Apache-2.0, see LICENSE.txt).

Local changes
- src/CedarDotNetFfi/Cargo.toml: cedar-policy moved from `git = ".../cedar", branch = "main"` (Cargo.lock had it at 4.5.0) to
  crates.io `=4.13.0`; Cargo.lock regenerated.
- tests/ and CedarDotNet.slnx copied; cedar-integration-tests/ is a plain copy (no nested git) of
  https://github.com/cedar-policy/cedar-integration-tests at 858d8bdc9ad4abd41020544f798a327bcf741b7e — the commit upstream pins.
  (Its latest commit adds *.cedar.json files that the upstream test loader cannot parse; unrelated to Cedar itself.)

Verification (Linux x64, .NET SDK 10.0.401, rustc 1.94.1)
- `cargo build --release` in src/CedarDotNetFfi: compiles unchanged FFI code against cedar-policy 4.13.0.
- `dotnet test tests/CedarDotNet.UnitTests`: 27/27 pass, including the Cedar integration corpus.
- Running library reports GetSdkVersion() = 4.13.0, GetLangVersion() = 4.5.

Build the native library (not committed):  cd src/CedarDotNetFfi && ./build-linux.sh
