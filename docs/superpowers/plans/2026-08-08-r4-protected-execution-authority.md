# R4 Protected Execution Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and independently gate a fixed-ID, root-owned, one-shot R4 protected execution authority that runs only a sealed checkout and hermetic Python 3.11 closure, preserves legacy R4, and defers privileged installation and custody rollout to separately approved stages.

**Architecture:** Python builds only an adversarial offline candidate and verifies protected descriptors; a local Swift package using Apple frameworks owns bootstrap, descriptor-relative copy, sealing, receipt signing, and verification. Ordinary tests inject disposable filesystem, code-signature, Mach-O, UUID, clock, and process adapters, so they require no sudo; only the installed Developer-ID-signed helper selects production adapters. The existing R4 Task 14 provides exact review evidence; a new exact-HEAD approval is required for protected bootstrap/provision/verify, and only then can the existing custody Task 15 seek its distinct approval.

**Tech Stack:** Swift 5.9+ (`Foundation`, `CryptoKit`, `Darwin`, `Security`), Python 3.11 standard library and repository canonical JSON/Ed25519 facilities, JSON Schema Draft 2020-12, XCTest, `unittest`, Ruff, Git plumbing, macOS code-signing and Mach-O inspection through recorded adapters.

---

## Safety and authority boundaries

- Work only in `/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd`. Ordinary commands must never traverse or mutate `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`, any production custody root, or `/Library`.
- The checkout, shell environment, Homebrew Python, Git configuration, and candidate directory are adversarial. Python and normal R4 commands cannot spawn the provisioner, acquire privilege, write root-owned state, or generate a production key.
- Fixed literals are namespace `org.mlb-walkforward.r4.protected-execution-authority/v1`, authority ID `r4-disjoint-custody-v2-execution-v1`, and protected root `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`. No environment/config/CLI/current-directory override exists.
- `FakeFileSystem` rejects mutations outside its temporary fixture root. `RecordingProcessRunner` rejects network tools and all executables except fixture tools or read-only system-inspection adapters. Unit tests use `TestCodeSignatureVerifier`, never a Developer ID.
- Every implementation change is RED → focused RED command → minimal GREEN → focused GREEN → compatibility command → listed commit. No task adds `resume`, `adopt`, `recover`, `repair`, `clean`, `force`, `overwrite`, or implicit cleanup.
- The dirty Task3 files remain owned by their existing patch until Task 0's separate review commit. Never reset, checkout, restore, or mix them with protected-authority files.

## File map and fixed interfaces

| Path | Responsibility |
| --- | --- |
| `schemas/r4_protected_execution_manifest.schema.json` | Closed canonical authority manifest. |
| `schemas/r4_hermetic_python311_runtime.schema.json` | Closed offline runtime/dependency closure. |
| `schemas/r4_task14_protected_execution_approval.schema.json` | Closed exact Task 14 envelope. |
| `schemas/r4_protected_execution_receipt.schema.json` | Closed signed receipt. |
| `schemas/r4_protected_execution_keyring.schema.json` | Closed keyring and revocation records. |
| `config/r4_protected_python311.lock.json` | Exact standalone Python and wheel URLs, versions, filenames, and SHA-256 values. |
| `scripts/prepare_r4_protected_runtime.py` | Offline-only safe extractor/installer from an already downloaded locked artifact set. |
| `scripts/build_r4_protected_candidate.py` | Untrusted offline candidate builder; never creates authority. |
| `scripts/build_r4_task14_approval.py` | Independently revalidates candidate evidence and emits the exact envelope. |
| `tools/r4_protected_execution_authority/Package.swift` | Swift package with no remote packages. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Constants,CanonicalJSON,Models,POSIX,Adapters,CLI,Bootstrap,CandidateTraversal,MachO,Provision,Keyring,Verify,main}.swift` | Privileged helper, split by capability. |
| `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{Fakes,CanonicalJSON,CLI,Bootstrap,CandidateTraversal,MachO,ProvisionState,Verify,Keyring}Tests.swift` | XCTest fake-root tests. |
| `mlb_backtest/v52_r4_protected_execution.py` | Read-only zero-argument protected adapter. |
| `mlb_backtest/v52_r4_disjoint_issuer.py` | Fixed protected branch while retaining Task3 legacy branch. |
| `tests/test_prepare_r4_protected_runtime.py` | Runtime lock, safe extraction, offline installation, import, and no-network tests. |
| `tests/test_build_r4_protected_candidate.py` | Candidate and envelope/Git plumbing tests. |
| `tests/test_r4_protected_execution_authority.py` | Disposable cross-language state/fault/process tests. |
| `tests/test_v52_r4_protected_execution.py` | Python adapter and issuer integration tests. |
| `scripts/run_task13_exact_suite.py`, `tests/test_task13_exact_suite_runner.py` | Append protected tests once in stable order. |

All later tasks use these names:

```swift
enum ProtectedConstants {
    static let namespace = "org.mlb-walkforward.r4.protected-execution-authority/v1"
    static let authorityID = "r4-disjoint-custody-v2-execution-v1"
    static let productionRoot = "/Library/Application Support/mlb-walkforward-lab/protected-execution-authority"
}
protocol FileSystem {
    func openDirectory(_ path: String, noFollow: Bool) throws -> DirectoryHandle
    func readRegular(_ parent: DirectoryHandle, _ name: String) throws -> RegularHandle
    func makeDirectory(_ parent: DirectoryHandle, _ name: String, mode: UInt16) throws -> DirectoryHandle
    func renameExclusive(_ from: DirectoryHandle, _ source: String, _ to: DirectoryHandle, _ destination: String) throws
    func fsync(_ handle: any DescriptorHandle) throws
    func seal(_ handle: any DescriptorHandle) throws
    func verifySealed(_ handle: any DescriptorHandle, expected: SealedMetadata) throws
}
```

```python
@dataclass(frozen=True)
class ProtectedExecutionBinding:
    namespace: str
    authority_id: str
    receipt_sha256: str
    manifest_sha256: str
    runtime_manifest_sha256: str
    git_head: str
    git_tree: str
    required_blob_set_sha256: str
    repository_clean: bool
    protected_python_sha256: str
    protected_python_identity: ProtectedFileIdentity
    public_key_sha256: str

def capture_execution_binding() -> ProtectedExecutionBinding: ...
```

### Task 0: Preserve and finish the pre-existing legacy Task3 hardening patch

**Files:**
- Modify only after review: `mlb_backtest/v52_r4_disjoint_execution.py`
- Modify only after review: `tests/test_v52_r4_disjoint_execution.py`

- [ ] **Step 1: Record dirty scope without changing it (2 minutes).**

Run:

```bash
git status --short
git diff --check -- mlb_backtest/v52_r4_disjoint_execution.py tests/test_v52_r4_disjoint_execution.py
git diff -- mlb_backtest/v52_r4_disjoint_execution.py tests/test_v52_r4_disjoint_execution.py
```

Expected: exactly those two pre-existing files are dirty; the patch disables repository-controlled Git helpers including `core.fsmonitor`, checks submodules, and rechecks `(HEAD, tree)` after final rebind. Do not use destructive Git commands.

- [ ] **Step 2: Prove the legacy regression suite first (3 minutes).**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_execution -v
```

Expected: PASS, including fsmonitor suppression and HEAD-advance-after-rebind tests. Any failure requires a focused legacy test and minimal legacy-only correction.

- [ ] **Step 3: Commit legacy hardening separately (3 minutes).**

Keep this public interface exactly unchanged:

```python
def capture_execution_binding(
    repository: Path, executable: Path, *, target_volume_uuid: str, supersession_sha256: str
) -> ExecutionBinding: ...
```

Run:

```bash
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_execution.py tests/test_v52_r4_disjoint_execution.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_execution.py tests/test_v52_r4_disjoint_execution.py
git commit -m "fix: harden legacy r4 execution binding"
```

Expected: one commit with only these two files. This finishes the patch rather than superseding it; protected work never changes the Task3 meaning of `repository_clean`.

### Task 1: Close policy, canonical JSON, and five evidence schemas

**Files:**
- Create: five `schemas/r4_*protected*.schema.json` files from the file map
- Create: `tools/r4_protected_execution_authority/Package.swift`
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Constants,CanonicalJSON,Models}.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CanonicalJSONTests.swift`

- [ ] **Step 1: Write failing closed-decoding tests (4 minutes).**

```swift
func testCanonicalJSONRejectsFloatDuplicateKeyAndNewline() throws {
    for bytes in [#"{"kind":"R4_PROTECTED_EXECUTION_MANIFEST_V1","x":1.0}"#, #"{"a":1,"a":2}"#, "{}\n"] {
        XCTAssertThrowsError(try CanonicalJSON.decode(bytes: Array(bytes.utf8)))
    }
}
func testManifestRejectsUnknownOrReorderedEntryPaths() throws {
    XCTAssertThrowsError(try AuthorityManifest.decode(canonical("{\\"unknown\\":true}")))
    XCTAssertThrowsError(try AuthorityManifest.decode(manifest(entries: ["checkout/z", "checkout/a"])))
}
```

Also validate schema fixtures rejecting missing/extra properties, uppercase hash, duplicate/reordered paths, duplicate/reordered helper hashes, and a namespace or authority ID differing from the fixed literal.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter CanonicalJSONTests`

Expected: FAIL because package/decoder/models do not exist.

- [ ] **Step 3: Implement strict canonical types and schemas (5 minutes).**

```swift
enum CanonicalJSON {
    static func decode(bytes: [UInt8]) throws -> JSONValue {
        guard !bytes.contains(10), let value = StrictJSONParser(bytes).parseDocument() else {
            throw ProvisionError.nonCanonicalJSON
        }
        guard encode(value) == bytes else { throw ProvisionError.nonCanonicalJSON }
        return value
    }
    static func digestOmittingSHA256(_ object: JSONObject) throws -> SHA256Digest {
        try SHA256Digest(bytes: encode(try object.removingExactly("sha256")))
    }
}
struct AuthorityManifest: Equatable {
    let namespace: String; let authorityID: String; let git: GitEvidence
    let entries: [ManifestEntry]; let runtimeManifestSHA256: SHA256Digest; let sha256: SHA256Digest
    static func decode(_ bytes: [UInt8]) throws -> AuthorityManifest
}
```

Implement `AuthorityManifest.decode` in this task as a closed-key parser: reject duplicate keys, floats/exponents, invalid UTF-8, newline bytes, unknown keys, unordered arrays, duplicate paths, invalid lower-case hashes, and mismatched self-digest. Every schema uses `additionalProperties: false`, required fields, fixed `kind`/namespace/authority ID, exact hex patterns, and ordered arrays. The keyring schema has closed `oneOf` branches for `R4_PROTECTED_EXECUTION_KEYRING_V1` and `R4_PROTECTED_EXECUTION_REVOCATION_V1`.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter CanonicalJSONTests
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_protocols -v
git diff --check
git add schemas/r4_protected_execution_manifest.schema.json schemas/r4_hermetic_python311_runtime.schema.json schemas/r4_task14_protected_execution_approval.schema.json schemas/r4_protected_execution_receipt.schema.json schemas/r4_protected_execution_keyring.schema.json tools/r4_protected_execution_authority
git commit -m "feat: define protected execution evidence"
```

Expected: GREEN; `Package.swift` has zero external dependencies and no package resolution command is used.

### Task 2: Pin and prepare the runtime, then build the offline candidate and Task 14 envelope

**Files:**
- Create: `config/r4_protected_python311.lock.json`
- Create: `scripts/prepare_r4_protected_runtime.py`
- Create: `scripts/build_r4_protected_candidate.py`
- Create: `scripts/build_r4_task14_approval.py`
- Create: `tests/test_prepare_r4_protected_runtime.py`
- Create: `tests/test_build_r4_protected_candidate.py`

- [ ] **Step 1: Write RED candidate and Git-evidence tests (5 minutes).**

```python
def test_candidate_builder_emits_sorted_exhaustive_manifest_without_network(self) -> None:
    result = build_candidate(source=self.repo, offline_runtime=self.runtime, output=self.root / "candidate")
    self.assertEqual(result.manifest["entries"], sorted(result.manifest["entries"], key=lambda row: row["path"]))
    self.assertEqual(result.processes, ())
    self.assertEqual(result.network_attempts, 0)

def test_task14_envelope_rejects_dirty_detached_hooked_or_mismatched_git(self) -> None:
    for repository in (self.dirty_repo(), self.detached_repo(), self.hooked_repo(), self.changed_blob_repo()):
        with self.assertRaisesRegex(ValueError, "Task 14 Git evidence"):
            build_task14_approval(repository, self.candidate, self.helper)

def test_runtime_preparation_is_offline_exact_and_rejects_archive_escape(self) -> None:
    prepared = prepare_runtime(lock=self.lock, downloads=self.downloads, output=self.runtime)
    self.assertEqual(prepared.python_version, "3.11.15")
    self.assertEqual(prepared.network_attempts, 0)
    with self.assertRaisesRegex(ValueError, "archive path"):
        prepare_runtime(lock=self.lock, downloads=self.archive_with_dotdot, output=self.root / "bad")

def test_candidate_rejects_live_file_swap_or_head_change_after_head_tree_enumeration(self) -> None:
    self.arm_source_swap_after("git-ls-tree")
    with self.assertRaisesRegex(ValueError, "tracked HEAD candidate changed"):
        build_candidate(source=self.repo, offline_runtime=self.runtime, output=self.root / "candidate")
```

The fixture uses only fixed `/usr/bin/git` built-ins with `--no-replace-objects`, `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `core.untrackedCache=false`, and `--no-ext-diff`; assert configured hooks/fsmonitor never run. Runtime tests deny sockets and allow a subprocess only for the freshly extracted pinned Python with `-I -s -E -m pip --no-index --no-deps --find-links "$downloads"`.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate -v`

Expected: FAIL because runtime preparation and both builders are absent.

- [ ] **Step 3: Commit the exact runtime lock and implement offline preparation (5 minutes).**

The closed lock records exact URLs as well as these filenames and SHA-256 values:

```json
{
  "kind": "R4_PROTECTED_PYTHON311_LOCK_V1",
  "python": ["cpython-3.11.15+20260807-aarch64-apple-darwin-install_only.tar.gz", "f7783d9a56a75d91554c08409a674e8678d56d42555fdbb365d8190d5cc93659"],
  "wheels": [
    ["attrs-25.4.0-py3-none-any.whl", "adcf7e2a1fb3b36ac48d97835bb6d8ade15b8dcce26aba8bf1d14847b57a3373"],
    ["cffi-2.0.0-cp311-cp311-macosx_11_0_arm64.whl", "2de9a304e27f7596cd03d16f1b7c72219bd944e99cc52b84d0145aefb07cbd3c"],
    ["cryptography-46.0.5-cp311-abi3-macosx_10_9_universal2.whl", "351695ada9ea9618b3500b490ad54c739860883df6c1f555e088eaf25b1bbaad"],
    ["jsonschema-4.25.1-py3-none-any.whl", "3fba0169e345c7175110351d456342c364814cfcf3b964ba4587f22915230a63"],
    ["jsonschema_specifications-2025.9.1-py3-none-any.whl", "98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe"],
    ["numpy-1.26.4-cp311-cp311-macosx_11_0_arm64.whl", "edd8b5fe47dab091176d21bb6de568acdd906d1887a4584a15a9a96a1dca06ef"],
    ["pycparser-3.0-py3-none-any.whl", "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992"],
    ["referencing-0.37.0-py3-none-any.whl", "381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231"],
    ["rpds_py-0.30.0-cp311-cp311-macosx_11_0_arm64.whl", "dc4f992dfe1e2bc3ebc7444f6c7051b4bc13cd8e33e43511e8ffd13bf407010d"],
    ["scipy-1.16.3-cp311-cp311-macosx_12_0_arm64.whl", "8be1ca9170fcb6223cc7c27f4305d680ded114a1567c0bd2bfcbf947d1b17511"],
    ["typing_extensions-4.15.0-py3-none-any.whl", "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548"]
  ]
}
```

Use these exact immutable/content-addressed URLs in the real lock, in the same order as the rows above:

```text
https://github.com/astral-sh/python-build-standalone/releases/download/20260807/cpython-3.11.15%2B20260807-aarch64-apple-darwin-install_only.tar.gz
https://files.pythonhosted.org/packages/3a/2a/7cc015f5b9f5db42b7d48157e23356022889fc354a2813c15934b7cb5c0e/attrs-25.4.0-py3-none-any.whl
https://files.pythonhosted.org/packages/4f/8b/f0e4c441227ba756aafbe78f117485b25bb26b1c059d01f137fa6d14896b/cffi-2.0.0-cp311-cp311-macosx_11_0_arm64.whl
https://files.pythonhosted.org/packages/f7/81/b0bb27f2ba931a65409c6b8a8b358a7f03c0e46eceacddff55f7c84b1f3b/cryptography-46.0.5-cp311-abi3-macosx_10_9_universal2.whl
https://files.pythonhosted.org/packages/bf/9c/8c95d856233c1f82500c2450b8c68576b4cf1c871db3afac5c34ff84e6fd/jsonschema-4.25.1-py3-none-any.whl
https://files.pythonhosted.org/packages/41/45/1a4ed80516f02155c51f51e8cedb3c1902296743db0bbc66608a0db2814f/jsonschema_specifications-2025.9.1-py3-none-any.whl
https://files.pythonhosted.org/packages/1a/2e/151484f49fd03944c4a3ad9c418ed193cfd02724e138ac8a9505d056c582/numpy-1.26.4-cp311-cp311-macosx_11_0_arm64.whl
https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl
https://files.pythonhosted.org/packages/2c/58/ca301544e1fa93ed4f80d724bf5b194f6e4b945841c5bfd555878eea9fcb/referencing-0.37.0-py3-none-any.whl
https://files.pythonhosted.org/packages/94/ba/24e5ebb7c1c82e74c4e4f33b2112a5573ddc703915b13a073737b59b86e0/rpds_py-0.30.0-cp311-cp311-macosx_11_0_arm64.whl
https://files.pythonhosted.org/packages/7c/89/d70e9f628749b7e4db2aa4cd89735502ff3f08f7b9b27d2e799485987cd9/scipy-1.16.3-cp311-cp311-macosx_12_0_arm64.whl
https://files.pythonhosted.org/packages/18/67/36e9267722cc04a6b9f15c7f3441c2363321a3ea07da7ae0c0707beb2a9c/typing_extensions-4.15.0-py3-none-any.whl
```

The real lock stores each row as `{"filename": ..., "url": ..., "sha256": ...}` in the exact Python-then-wheel order shown above; version, filename, URL, and digest are all signed inputs to `runtime_manifest.json`. Acquisition is a manual development-only download into `.artifacts/r4-protected-python311/downloads`; neither committed code nor the helper downloads. `prepare_runtime` requires the download directory to equal the lock, verifies all hashes before extraction, permits only normalized regular files/directories with no hard links, safely extracts into a fresh output, and runs only the extracted interpreter to install every locked wheel offline. It then proves imports of `cryptography`, `jsonschema`, `numpy`, and `scipy` with `sys.path` confined to the runtime root. Missing/extra archives, sdists, dependency resolution, builds, indexes, caches, user site, sockets, or external loader paths fail.

- [ ] **Step 4: Implement tracked-closure candidate and envelope construction (5 minutes).**

```python
def build_candidate(*, source: Path, offline_runtime: Path, output: Path) -> CandidateBuild:
    evidence = read_git_evidence(source)
    checkout = copy_tracked_head_tree_nofollow(source, evidence.tracked_regular_files, output / "checkout")
    runtime = copy_tree_nofollow(open_verified_directory(offline_runtime), output / "python311")
    manifest = build_authority_manifest(output, git_evidence=evidence)
    runtime_manifest = build_runtime_manifest(runtime, artifact_source="OFFLINE_PINNED_ARTIFACT")
    write_canonical(output / "authority-manifest.json", manifest)
    write_canonical(output / "runtime-manifest.json", runtime_manifest)
    return CandidateBuild(output, manifest, runtime_manifest, (), 0)

def build_task14_approval(repository: Path, candidate: Path, helper: Path) -> dict[str, object]:
    evidence = read_git_evidence(repository)
    return canonical_with_digest({"kind": "R4_TASK14_PROTECTED_EXECUTION_APPROVAL_V1",
        "namespace": NAMESPACE, "authority_id": AUTHORITY_ID, "reviewed_head": evidence.head,
        "reviewed_tree": evidence.tree, "candidate_manifest_sha256": sha256_file(candidate / "authority-manifest.json"),
        "runtime_manifest_sha256": sha256_file(candidate / "runtime-manifest.json"),
        "provisioner_swift_source_sha256": digest_swift_sources(helper),
        "provisioner_binary_sha256": sha256_file(helper / "provisioner"),
        "provisioner_designated_requirement_sha256": sha256_file(helper / "provisioner.codesign-requirement.txt"),
        "helper_hashes": ordered_helper_hashes(helper), "required_blob_set_sha256": evidence.required_blob_set_sha256,
        "exact_suite_log_sha256": sha256_file(helper / "exact-suite.log")})
```

The builder accepts absolute paths only, rejects symlinks/hard links/special files, writes solely below its caller-selected untrusted output, and never runs pip/brew/compiler/downloader/network. `read_git_evidence` resolves `HEAD^{commit}` and `HEAD^{tree}`, enumerates `git ls-tree -r --full-tree HEAD`, requires clean index/worktree/untracked state, and records one `(path, blob, sha256)` row per tracked regular file. `copy_tracked_head_tree_nofollow` copies only those rows from held live descriptors, compares each held file to its enumerated blob SHA-256 before and after copy, then re-runs `read_git_evidence` and rejects changed HEAD/tree/rows. Reject submodules, tracked symlinks, `.git`, ignored/untracked files, missing paths, extra checkout objects, and any Git tree type other than regular file/directory.

- [ ] **Step 5: Run GREEN and commit (3 minutes).**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate -v
/opt/homebrew/bin/python3.11 -m ruff check scripts/prepare_r4_protected_runtime.py scripts/build_r4_protected_candidate.py scripts/build_r4_task14_approval.py tests/test_prepare_r4_protected_runtime.py tests/test_build_r4_protected_candidate.py
git diff --check
git add config/r4_protected_python311.lock.json scripts/prepare_r4_protected_runtime.py scripts/build_r4_protected_candidate.py scripts/build_r4_task14_approval.py tests/test_prepare_r4_protected_runtime.py tests/test_build_r4_protected_candidate.py
git commit -m "feat: build offline r4 protected candidate"
```

Expected: GREEN; candidate artifacts remain untrusted and outside the authority root.

### Task 3: Establish Swift command grammar and disposable adapters

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{POSIX,Adapters,CLI,main}.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{Fakes,CLI}Tests.swift`

- [ ] **Step 1: Write RED closed-command and path-capability tests (4 minutes).**

```swift
func testCLIRejectsEveryUnclosedOptionBeforeMutation() throws {
    for argv in [["provision", "--force"], ["verify", "--authority-id", "other"], ["provision", "relative"], ["bootstrap-install", "--candidate-root", "/x"]] {
        XCTAssertThrowsError(try CommandLineParser.parse(argv))
    }
    XCTAssertEqual(fakeFS.mutations, [])
}
func testEnvironmentCannotSelectProductionRoot() throws {
    XCTAssertThrowsError(try ProductionAdapters(environment: ["R4_ROOT": "/tmp/x", "DYLD_INSERT_LIBRARIES": "x"]))
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter CLITests`

Expected: FAIL because parser/adapters do not exist.

- [ ] **Step 3: Implement fixed grammar and adapters (5 minutes).**

```swift
enum Command: Equatable {
    case bootstrapInstall(envelope: AbsolutePath, expectedSHA256: SHA256Digest)
    case provision(candidateRoot: AbsolutePath)
    case verify
}
static func parse(_ argv: [String]) throws -> Command {
    switch argv {
    case ["bootstrap-install", "--approval-envelope", let path, "--expected-approval-sha256", let digest]:
        return .bootstrapInstall(envelope: try AbsolutePath(path), expectedSHA256: try SHA256Digest(hex: digest))
    case ["provision", "--candidate-root", let path]:
        return .provision(candidateRoot: try AbsolutePath(path))
    case ["verify", "--authority-id", ProtectedConstants.authorityID]:
        return .verify
    default: throw ProvisionError.invalidArguments
    }
}
```

`ProductionAdapters` closes stdin, clears and rejects `PATH`, `PWD`, `HOME`, `PYTHONHOME`, `PYTHONPATH`, every `DYLD_*`, and inherited descriptors. It provides real filesystem/codesign/Mach-O/random/key adapters. Fakes implement the same protocols and prove fixture-root confinement.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter CLITests
swift build --package-path tools/r4_protected_execution_authority --disable-sandbox
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/POSIX.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/CLI.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/main.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CLITests.swift
git commit -m "feat: add closed protected provisioner cli"
```

Expected: GREEN; build runs without fetching a dependency.


### Task 4: Bootstrap-install the independently reviewed helper and root key

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Bootstrap.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/BootstrapTests.swift`

- [ ] **Step 1: Write RED bootstrap ordering and identity tests (5 minutes).**

```swift
func testBootstrapRejectsDigestSignatureSourceBinaryAndCandidateKeyMismatches() throws {
    for fixture in [wrongEnvelopeDigest, adHocSignature, wrongTeamID, sourceMismatch, binaryMismatch, candidateKey] {
        XCTAssertThrowsError(try bootstrap(fixture))
        XCTAssertFalse(fixture.fs.exists("bootstrap/R4_PROTECTED_EXECUTION_BOOTSTRAP_V1.json"))
    }
}
func testBootstrapPinsPublicKeyBeforeAnyReceiptAuthority() throws {
    try bootstrap(validFixture)
    XCTAssertEqual(validFixture.fs.events, [.generateKey, .writeBootstrapPolicy, .publishBootstrap,
        .sealPublicArtifacts, .signProbe, .sealPrivateKey, .reopenVerify])
}
```

Test absent bootstrap/control state, unexpected install-root children, non-root ownership, ACL, incorrect mode, missing `SF_IMMUTABLE`, and an installed-path invocation. Each rejects before a candidate read.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter BootstrapTests`

Expected: FAIL because bootstrap install is absent.

- [ ] **Step 3: Implement atomic root trust ceremony (5 minutes).**

```swift
func bootstrapInstall(_ request: BootstrapRequest, adapters: Adapters) throws {
    try requireNoBootstrapOrControlState(adapters.fs)
    let approval = try Task14Approval.readHeld(request.envelope, fs: adapters.fs)
    try approval.requireDigest(request.expectedSHA256)
    let selfIdentity = try adapters.codeSignature.verifyHeldExecutable()
    try approval.requireSelf(source: adapters.sourceDigest(), binary: selfIdentity.sha256,
                             requirement: selfIdentity.designatedRequirementSHA256)
    let stage = try adapters.fs.freshPrivateStaging("bootstrap")
    let key = try adapters.keys.generateEd25519()
    try stage.writeBootstrap(policy: BootstrapPolicy(approval: approval, publicKey: key.publicKey,
                                                     selfIdentity: selfIdentity), key: key)
    try stage.fsyncAll(); try stage.sealBootstrapPublicArtifacts()
    try key.signProbe(); try stage.sealPrivateKey()
    try stage.reopenAndVerifyBootstrap(); try stage.publishBootstrapExclusive(); try stage.fsyncParent()
}
```

Write exactly `bootstrap/R4_PROTECTED_EXECUTION_BOOTSTRAP_V1.json`, provisioner, requirement text/digest, public key/digest, private key, and initial-key-generation record. Publish `control/` root:wheel `0700` with keyring generation `00000001`; private keys are `0600`, other sealed files `0444`, sealed directories `0555`. Reopen all objects no-follow and prove owner, mode, system immutable flag, no ACL, digest, designated requirement, identity, and no extra child.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter BootstrapTests
swift test --package-path tools/r4_protected_execution_authority
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Bootstrap.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/BootstrapTests.swift
git commit -m "feat: bootstrap protected provisioner trust"
```

Expected: GREEN; a success never reads a candidate and every failing fixture leaves no bootstrap tree.

### Task 5: Copy the manifest closure with descriptor-relative no-follow operations

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/CandidateTraversal.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CandidateTraversalTests.swift`

- [ ] **Step 1: Write RED malformed-entry and pathname-swap tests (5 minutes).**

```swift
func testCopyRejectsEscapesUnicodeCaseCollisionsLinksAndSpecialFiles() throws {
    for entry in ["../x", "a//b", "A", "a", "e\u{301}", "é", symlink, fifo, socket, device, hardLinkedRegular] {
        XCTAssertThrowsError(try copyCandidate(entry))
        XCTAssertEqual(fakeFS.publishedAuthorities, [])
    }
}
func testCopyRebindsBeforeDuringAndAfterSecurityReads() throws {
    fakeFS.swapNameAfter(.sourceHash)
    XCTAssertThrowsError(try copyCandidate(validCandidate))
    XCTAssertTrue(fakeFS.quarantineExists)
}
```

Also reject missing/extra manifest rows, directory nonzero size, regular mode/hash/size drift, directory digest drift, source/destination device mismatch, and an attempted replacement after source read, copy, fsync, or destination reopen.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter CandidateTraversalTests`

Expected: FAIL because candidate traversal/copy is absent.

- [ ] **Step 3: Implement held-descriptor validation/copy (5 minutes).**

```swift
func copyEntry(_ entry: ManifestEntry, from source: DirectoryHandle, to destination: DirectoryHandle,
               fs: FileSystem) throws {
    try entry.path.requireNormalizedPOSIX()
    let held = try source.openRelative(entry.path, flags: [.noFollow, .closeOnExec])
    let before = try held.metadata(requireLinkCount: 1, expected: entry)
    try fs.copyRegularOrCreateDirectory(held, into: destination, mode: entry.mode)
    try fs.fsync(destination)
    let copied = try destination.openRelative(entry.path, flags: [.noFollow, .closeOnExec])
    try copied.requireSameManifestEntry(entry)
    try copied.requireReboundNameAndIdentity()
    try held.requireIdentity(before.identity)
}
```

Enumerate solely manifest rows in lexical order. Retain each parent descriptor and use `openat(O_NOFOLLOW|O_CLOEXEC)`; reject all types except directory/regular and every `nlink != 1`. For directories compute `SHA256("R4DIR\\0" + canonical immediate-child rows)`. Rebind source and destination before/after every read, copy, metadata change, and fsync; never transfer an accepted candidate pathname into authority state.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter CandidateTraversalTests
swift test --package-path tools/r4_protected_execution_authority
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/CandidateTraversal.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CandidateTraversalTests.swift
git commit -m "feat: copy protected closure by descriptor"
```

Expected: GREEN; no accepted file type, path, byte, or identity escapes the manifest.

### Task 6: Resolve hermetic Python and Mach-O dependency closure

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/MachO.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MachOTests.swift`

- [ ] **Step 1: Write RED dependency/environment tests (5 minutes).**

```swift
func testDependencyResolverAcceptsOnlySystemOrProtectedRoots() throws {
    XCTAssertNoThrow(try resolve("@loader_path/libx.dylib", loader: protectedLoader))
    for path in ["/opt/homebrew/lib/x.dylib", "/Users/a/x.dylib", "/Network/x.dylib", "relative.dylib", "@rpath/missing.dylib"] {
        XCTAssertThrowsError(try resolve(path, loader: protectedLoader))
    }
}
func testRuntimeRejectsDYLDPythonAndArtifactMismatch() throws {
    XCTAssertThrowsError(try validateRuntime(env: ["DYLD_LIBRARY_PATH": "/x"]))
    XCTAssertThrowsError(try validateRuntime(env: ["PYTHONPATH": "/x"]))
    XCTAssertThrowsError(try validateRuntime(artifactSHA256: wrongDigest))
}
```

Cover each `LC_LOAD_DYLIB`, `LC_LOAD_WEAK_DYLIB`, `LC_REEXPORT_DYLIB`, `@loader_path`, `@executable_path`, `@rpath`, missing/ambiguous RPATH, candidate symlink, unsigned signed-required Mach-O, and user-site import.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter MachOTests`

Expected: FAIL because resolver is absent.

- [ ] **Step 3: Implement held-object dependency closure (5 minutes).**

```swift
func validateMachOClosure(_ objects: [HeldMachO], root: CanonicalPath,
                          inspector: MachOInspector) throws -> SHA256Digest {
    let rows = try objects.flatMap { loader in
        try inspector.loadCommands(of: loader).filter(\.isDependency).map { command in
            let resolved = try resolve(command.installName, loader: loader,
                                       rpaths: inspector.rpaths(of: loader), root: root)
            guard resolved.hasPrefix("/usr/lib/") || resolved.hasPrefix("/System/") ||
                  resolved.hasPrefix(root.string) else { throw ProvisionError.externalDependency }
            return DependencyRow(loader: loader.relativePath, dependency: command.installName, resolved: resolved)
        }
    }
    return try CanonicalJSON.digest(rows.sorted())
}
```

Parse commands directly from held copied descriptors. Require all runtime objects under `closure/python311` except canonical `/usr/lib/` and `/System/` dependencies; reject Homebrew, user, developer-toolchain, candidate, network, relative, missing, ambiguous, symlinked, or external paths. Code-sign-check copied signed executables/frameworks and write the sorted dependency closure digest into the receipt inputs.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter MachOTests
swift test --package-path tools/r4_protected_execution_authority
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/MachO.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MachOTests.swift
git commit -m "feat: validate hermetic python closure"
```

Expected: GREEN; accepted rows resolve only under `/usr/lib`, `/System`, or the disposable protected root.

### Task 7: Sign receipt before sealing, publish once, and retain quarantine

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Provision.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ProvisionStateTests.swift`

- [ ] **Step 1: Write RED ordering/state tests (5 minutes).**

```swift
func testReceiptSignatureIsFsyncedBeforeSealAndRename() throws {
    try provision(validCandidate)
    XCTAssertEqual(fakeFS.events.suffix(6), [.writeReceipt, .fsyncReceipt, .writeSignature,
        .fsyncSignature, .sealTree, .renameExclusive])
}
func testAnyFaultQuarantinesAndPermanentlyRejectsSameID() throws {
    for seam in ProvisionSeam.allCases {
        let fixture = faultingFixture(after: seam)
        XCTAssertThrowsError(try provision(fixture))
        XCTAssertTrue(fixture.fs.quarantineExists)
        XCTAssertThrowsError(try provision(fixture.withoutFault))
    }
}
```

Test authority-exists, matching-quarantine-exists, unrecognized authority child, receipt signature length not 64, fsync failure, seal/readback failure, no-replace rename unavailable, signal at each transition, and post-publish revalidation failure.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter ProvisionStateTests`

Expected: FAIL because state machine is absent.

- [ ] **Step 3: Implement the terminal state machine (5 minutes).**

```swift
func provision(_ request: ProvisionRequest, adapters: Adapters) throws {
    try InstalledBootstrap.openAndRevalidate(adapters).requireFixedAuthorityAbsent()
    let stage = try adapters.fs.newAuthorityStaging(authorityID: ProtectedConstants.authorityID)
    do {
        let candidate = try CandidateValidator.openAndValidate(request.candidateRoot, bootstrap: adapters.bootstrap)
        try copyAndRevalidate(candidate, into: stage, adapters: adapters)
        let receipt = try Receipt.make(stage: stage, bootstrap: adapters.bootstrap)
        let signature = try adapters.keys.current().sign(receipt.canonicalBytes)
        try stage.writeAndFsync(receipt); try stage.writeAndFsync(signature)
        try stage.sealAndRevalidateCompleteTree(); try stage.publishExclusive(); try stage.revalidatePublished()
    } catch {
        try adapters.fs.quarantineWithoutDeletion(stage, authorityID: ProtectedConstants.authorityID)
        throw error
    }
}
```

Require ABSENT before the first candidate read; stage only in root-owned `0700` private sibling with generated UUID. Copy and revalidate all closure bytes, dependency digest, Task14 envelope, and Git evidence; make receipt from held staging descriptors. Write/fsync receipt before detached signature, seal all artifacts after both, then `renameatx_np(RENAME_EXCL)` and fsync parent. If quarantine rename cannot be proven, treat original owned staging as quarantine; never emulate exclusive rename or clear a flag.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter ProvisionStateTests
swift test --package-path tools/r4_protected_execution_authority
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Provision.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ProvisionStateTests.swift
git commit -m "feat: seal and publish protected authority"
```

Expected: GREEN; every non-success is a terminal forensic state with no same-ID retry.

### Task 8: Verify pinned key chain, rotation, and revocation

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Verify,Keyring}.swift`
- Create: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{Verify,Keyring}Tests.swift`

- [ ] **Step 1: Write RED verification/keyring tests (5 minutes).**

```swift
func testVerifyPinsBootstrapKeyBeforeReceiptAndClosure() throws {
    for mutation in [wrongBootstrapPublicKey, wrongReceiptSignature, changedPython, extraClosureEntry, wrongManifestDigest] {
        XCTAssertThrowsError(try verify(mutation))
    }
}
func testKeyringAndRevocationsAreContiguousSignedAndActive() throws {
    for records in [missingGeneration, duplicateGeneration, unsignedRotation, gapInRevocationSequence, revokedFinalState] {
        XCTAssertThrowsError(try verify(records))
    }
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter VerifyTests --filter KeyringTests`

Expected: FAIL because keyring/verification is absent.

- [ ] **Step 3: Implement fail-closed protected verification (5 minutes).**

```swift
func verifyActiveAuthority(_ root: DirectoryHandle, adapters: Adapters) throws -> VerifiedAuthority {
    let bootstrap = try BootstrapPolicy.openPinned(from: root, fs: adapters.fs)
    let keyring = try Keyring.loadContiguous(from: root, pinned: bootstrap.publicKey, fs: adapters.fs)
    let receipt = try Receipt.readVerified(root, publicKey: keyring.keyForReceipt, fs: adapters.fs)
    try RevocationLog.loadContiguous(root, keyring: keyring, fs: adapters.fs)
        .requireActive(ProtectedConstants.authorityID)
    return try VerifiedAuthority.revalidate(receipt: receipt, root: root, bootstrap: bootstrap, fs: adapters.fs)
}
```

Normal provision and Python expose no `control` write method. A separate root administrative ceremony creates only the next immutable eight-digit keyring generation, signs it with the preceding key, and retains old public keys for historical receipts; it never rewrites bootstrap public key or receipts. Revocation appends the next signed immutable sequence with only `ACTIVE`/`REVOKED` and closed codes `KEY_COMPROMISE`, `HELPER_COMPROMISE`, `MANIFEST_COMPROMISE`, `PLATFORM_ASSUMPTION_INVALID`.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter VerifyTests --filter KeyringTests
swift test --package-path tools/r4_protected_execution_authority
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Verify.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Keyring.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/VerifyTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/KeyringTests.swift
git commit -m "feat: verify protected keyring authority"
```

Expected: GREEN; receipt verification succeeds only with bootstrap pin, sealed exhaustive closure, contiguous key chain, and final active revocation state.


### Task 9: Add the unprivileged Python protected adapter

**Files:**
- Create: `mlb_backtest/v52_r4_protected_execution.py`
- Create: `tests/test_v52_r4_protected_execution.py`

- [ ] **Step 1: Write RED zero-argument, immutable-verification, and confinement tests (4 minutes).**

~~~python
def test_capture_has_no_candidate_or_python_path_parameters(self) -> None:
    self.assertEqual(tuple(inspect.signature(capture_execution_binding).parameters), ())

def test_capture_rejects_changed_python_extra_entry_and_live_worktree_change(self) -> None:
    authority = protected_fixture(self.root)
    self.assertTrue(capture_execution_binding_for_test(authority).repository_clean)
    mutate(authority / "authorities" / AUTHORITY_ID / "closure/python311/bin/python3.11")
    with self.assertRaises(ProtectedExecutionUnavailable):
        capture_execution_binding_for_test(authority)
~~~

Record descriptor/process calls and assert all reads are fixture-authority descendants; no linked worktree, Homebrew runtime, production custody root, provisioner process, or network call occurs.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_protected_execution -v`

Expected: FAIL because module is absent.

- [ ] **Step 3: Implement only read-only held-descriptor capture (5 minutes).**

~~~python
def capture_execution_binding() -> ProtectedExecutionBinding:
    return _capture_from_root(_PROTECTED_ROOT, _RealDescriptorOps())

def _capture_from_root(root: Path, ops: DescriptorOps) -> ProtectedExecutionBinding:
    verified = verify_protected_authority(root=root, ops=ops, expected_authority_id=AUTHORITY_ID)
    python = verified.open_regular("closure/python311/bin/python3.11")
    return ProtectedExecutionBinding(NAMESPACE, AUTHORITY_ID, verified.receipt_sha256,
        verified.manifest_sha256, verified.runtime_manifest_sha256, verified.git_head, verified.git_tree,
        verified.required_blob_set_sha256, True, python.sha256, python.identity, verified.public_key_sha256)
~~~

Repeat bootstrap pin, detached receipt signature, closed manifests, complete no-extra entry enumeration, mode/owner/ACL/`SF_IMMUTABLE`, active keyring/revocation, and descriptor rebind. Keep `capture_execution_binding_for_test(root, ops)` test-only and require a disposable root.

- [ ] **Step 4: Run GREEN and commit (3 minutes).**

~~~bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_protected_execution -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_protected_execution.py tests/test_v52_r4_protected_execution.py
git diff --check
git add mlb_backtest/v52_r4_protected_execution.py tests/test_v52_r4_protected_execution.py
git commit -m "feat: add protected r4 execution binding"
~~~

Expected: GREEN; protected `repository_clean` means exact sealed closure, never live `git status`.

## Required inter-plan checkpoint before Task 10

`mlb_backtest/v52_r4_disjoint_issuer.py` and `tests/test_v52_r4_disjoint_custody.py` do not exist at the start of this plan. Before Task 10, execute and independently approve Tasks 4–9 from `docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md` in their original order:

1. old-attempt read-only fingerprint;
2. seed-free append-only journal;
3. full-tree v2 inventory/receipt;
4. v2 contract and signed supersession anchor;
5. disjoint owner/layout/observer;
6. journaled issuance and six-artifact publication.

Those tasks retain their own RED/GREEN commands, commits, spec review, and quality review. They must consume the protected-execution interfaces only where their existing Task 3 dependency is referenced; they must not weaken or duplicate Tasks 0–9 above. Do not start Task 10 until the original Task 9 commit is clean and both reviews report no P0/P1/P2. Original-plan Tasks 10–13 then run after protected Task 10 so fresh verification, CLI admission, downstream contract admission, and the combined race/signal matrix all exercise the protected branch.

### Task 10: Select only the fixed protected issuer branch

**Files:**
- Modify: `mlb_backtest/v52_r4_disjoint_issuer.py`
- Modify: `tests/test_v52_r4_disjoint_execution.py`
- Modify: `tests/test_v52_r4_protected_execution.py`

- [ ] **Step 1: Write RED branch/legacy-isolation tests (3 minutes).**

~~~python
from unittest.mock import patch

def test_fixed_protected_authority_uses_zero_arg_capture_only(self) -> None:
    with (
        patch("mlb_backtest.v52_r4_disjoint_issuer.capture_protected_execution_binding") as protected,
        patch("mlb_backtest.v52_r4_disjoint_issuer.capture_execution_binding") as legacy,
    ):
        build_execution_context(authority_id=PROTECTED_AUTHORITY_ID)
    protected.assert_called_once_with()
    legacy.assert_not_called()
~~~

Add complementary legacy test asserting original repository/executable arguments are passed and protected capture is never called.

- [ ] **Step 2: Run RED, implement selector, run GREEN, and commit (5 minutes).**

~~~python
def _capture_for_r4_authority(authority_id: str, legacy: LegacyCaptureInputs) -> ExecutionBinding | ProtectedExecutionBinding:
    if authority_id == PROTECTED_AUTHORITY_ID:
        return capture_protected_execution_binding()
    return capture_execution_binding(legacy.repository, legacy.executable,
        target_volume_uuid=legacy.target_volume_uuid, supersession_sha256=legacy.supersession_sha256)
~~~

~~~bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_execution tests.test_v52_r4_protected_execution tests.test_v52_r4_disjoint_custody -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_execution.py tests/test_v52_r4_protected_execution.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_execution.py tests/test_v52_r4_protected_execution.py
git commit -m "feat: select protected r4 issuer binding"
~~~

Expected: GREEN; invoke only held `closure/python311/bin/python3.11` with imports confined to protected checkout/runtime roots, both before startup and before sensitive issuance, while legacy Task3 interfaces remain unchanged.

### Task 11: Run disposable fault, race, signal, process, and network matrix

**Files:**
- Create: `tests/test_r4_protected_execution_authority.py`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`
- Modify production Swift only for a demonstrated RED regression.

- [ ] **Step 1: Write RED complete failure matrix (5 minutes).**

~~~python
for seam in ("candidate_open", "candidate_rebind", "copy", "copy_fsync", "runtime_validate",
             "receipt_sign", "receipt_fsync", "signature_fsync", "seal", "seal_readback",
             "rename_exclusive", "parent_fsync", "published_rebind"):
    with self.subTest(seam=seam):
        fixture = disposable_authority_fixture(self.root, fail_after=seam)
        self.assertNotEqual(run_provisioner(fixture).returncode, 0)
        self.assertEqual(fixture.quarantine_count, 1)
        self.assertNotEqual(run_provisioner(fixture.without_fault()).returncode, 0)
~~~

Inject SIGINT/SIGTERM and candidate swaps at every seam; also test unavailable exclusive rename, system-flag readback failure, ACL, bad code signature, and every disallowed Mach-O root. Record open/stat/rename/chflags/ACL/process calls; writes must be fixture-root-only, allowed system inspection read-only, and any network syscall/tool must fail.

- [ ] **Step 2: Run RED, make each demonstrated minimal fix, then GREEN (5 minutes per seam).**

~~~bash
swift test --package-path tools/r4_protected_execution_authority
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_r4_protected_execution_authority tests.test_v52_r4_protected_execution -v
~~~

Expected: initially one or more seam cases may fail; retain each regression, add only its rebind/state guard, rerun the named case until PASS. No fix may delete/unseal/retry state or introduce pathname TOCTOU.

- [ ] **Step 3: Commit matrix (3 minutes).**

~~~bash
git diff --check
git add tests/test_r4_protected_execution_authority.py tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner
git commit -m "test: seal protected authority failure matrix"
~~~

Expected: every injected interruption is retained non-executable forensic state.

### Task 12: Wire exact suite and run implementation gates

**Files:**
- Modify: `scripts/run_task13_exact_suite.py`
- Modify: `tests/test_task13_exact_suite_runner.py`

- [ ] **Step 1: Write RED stable-order test (3 minutes).**

~~~python
def test_protected_children_append_once_after_existing_r4_children() -> None:
    suffix = ("tests.test_prepare_r4_protected_runtime", "tests.test_build_r4_protected_candidate",
              "tests.test_r4_protected_execution_authority", "tests.test_v52_r4_protected_execution")
    self.assertEqual(CHILD_MODULES[-4:], suffix)
    self.assertTrue(all(CHILD_MODULES.count(name) == 1 for name in suffix))
~~~

- [ ] **Step 2: Run RED, append exact suffix, GREEN, and commit (4 minutes).**

~~~bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_task13_exact_suite_runner -v
/opt/homebrew/bin/python3.11 -m ruff check scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git diff --check
git add scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "test: add protected authority to exact suite"
~~~

Expected: GREEN; existing child order is unchanged and exact suite never invokes protected/custody roots.


### Task 13: Complete Task 14 evidence, reviews, and exact gates

**Files:**
- Review: all commits after Task 0
- Create outside Git only: disposable candidate, exact-suite log, `task14-approval.json`
- Verify: `docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md`

- [ ] **Step 1: Run focused, Swift, Ruff, exact, diff, and protected-path gates (5 minutes).**

~~~bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_execution tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate tests.test_r4_protected_execution_authority tests.test_v52_r4_protected_execution tests.test_v52_r4_disjoint_custody -v
swift test --package-path tools/r4_protected_execution_authority
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_protected_execution.py mlb_backtest/v52_r4_disjoint_issuer.py scripts/prepare_r4_protected_runtime.py scripts/build_r4_protected_candidate.py scripts/build_r4_task14_approval.py tests/test_prepare_r4_protected_runtime.py tests/test_build_r4_protected_candidate.py tests/test_r4_protected_execution_authority.py tests/test_v52_r4_protected_execution.py
git diff --check
git diff --name-only 5d0be9d9eee3935af83d22c8df5ceb2cce66aa9e..HEAD -- audit data reports protocols
rg -n --glob '*.py' --glob '*.swift' 'sudo|AuthorizationExecuteWithPrivileges|Process\(|/Library/Application Support/mlb-walkforward-lab/protected-execution-authority' mlb_backtest scripts tools tests
~~~

Run the current R4 plan's Task 14 exact-suite command unchanged and capture it under a HEAD-bound evidence directory:

```bash
reviewed_head="$(git rev-parse HEAD)"
evidence_root="/private/tmp/r4-protected-task14-${reviewed_head}"
test ! -e "$evidence_root"
mkdir -m 700 "$evidence_root"
set +e
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/run_task13_exact_suite.py \
  --repo . \
  --v51-input-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --old-v52-evidence-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --chromium-executable '/Users/taejunoh/.cache/puppeteer/chrome-headless-shell/mac_arm-146.0.7680.153/chrome-headless-shell-mac-arm64/chrome-headless-shell' \
  --chromium-sha256 454577a889a2c0b698c09f0ca8db047ea2e6cd604a42c36dc09b375c81362518 \
  --chromium-bundle-sha256 2ea5035c2085aaf0fb70be6d2949f51eeb42affaac47772044ddaa727cb4c253 \
  --python /opt/homebrew/bin/python3.11 >"$evidence_root/exact-suite.log" 2>&1
exact_suite_status=$?
set -e
printf '%s\n' "$exact_suite_status" >"$evidence_root/exact-suite.exit"
test "$exact_suite_status" -eq 0
/usr/bin/shasum -a 256 "$evidence_root/exact-suite.log" >"$evidence_root/exact-suite.log.sha256"
```

Expected: all PASS, exit file `0`, no generated audit/data/report/protocol change, and the scan finds only fixed constants/test assertions—not Python privileged/network launchers or mutable-root overrides.

- [ ] **Step 2: Generate and independently verify concrete Task 14 envelope (5 minutes).**

~~~bash
candidate_root="$evidence_root/candidate-input"
runtime_root="$evidence_root/offline-python311"
helper_build="$PWD/tools/r4_protected_execution_authority/.build/release"
helper_binary="$helper_build/R4ProtectedExecutionProvisioner"
approved_signing_identity='Developer ID Application: Taejun Oh (3BMF4LM6TM)'
approved_signing_sha1='4C6503D0A78A9F51C86CC7C9683115E74A4902A7'
mkdir -m 700 "$candidate_root"
swift build -c release --package-path tools/r4_protected_execution_authority
test -x "$helper_binary"
test "$(/usr/bin/security find-identity -v -p codesigning | /usr/bin/awk -v sha1="$approved_signing_sha1" -v name="$approved_signing_identity" '$2 == sha1 && index($0, name) { count += 1 } END { print count + 0 }')" -eq 1
/usr/bin/ditto "$helper_binary" "$evidence_root/provisioner"
/usr/bin/codesign --force --options runtime --timestamp --sign "$approved_signing_identity" "$evidence_root/provisioner"
/usr/bin/codesign --verify --strict --deep --verbose=4 "$evidence_root/provisioner"
/usr/bin/codesign -d -r- "$evidence_root/provisioner" >"$evidence_root/provisioner.codesign-requirement.txt" 2>&1
/usr/bin/codesign -dvv "$evidence_root/provisioner" >"$evidence_root/provisioner.codesign-details.txt" 2>&1
/usr/bin/grep -Fx 'TeamIdentifier=3BMF4LM6TM' "$evidence_root/provisioner.codesign-details.txt"
/usr/bin/grep -F 'Developer ID Application: Taejun Oh (3BMF4LM6TM)' "$evidence_root/provisioner.codesign-details.txt"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/prepare_r4_protected_runtime.py --lock config/r4_protected_python311.lock.json --downloads .artifacts/r4-protected-python311/downloads --output "$runtime_root"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/build_r4_protected_candidate.py --source "$PWD" --offline-runtime "$runtime_root" --output "$candidate_root/candidate"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/build_r4_task14_approval.py --repository "$PWD" --candidate "$candidate_root/candidate" --helper-build "$evidence_root" --exact-suite-log "$evidence_root/exact-suite.log" --output "$evidence_root/task14-approval.json"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/build_r4_task14_approval.py --verify-only --repository "$PWD" --candidate "$candidate_root/candidate" --helper-build "$evidence_root" --approval "$evidence_root/task14-approval.json"
/usr/bin/shasum -a 256 "$evidence_root/task14-approval.json" >"$evidence_root/task14-approval.sha256"
~~~

Expected: the only signing identity is SHA-1 `4C6503D0A78A9F51C86CC7C9683115E74A4902A7`, `Developer ID Application: Taejun Oh (3BMF4LM6TM)`; Apple Development identities and environment-selected identities are not accepted. The evidence has exact HEAD/tree, candidate/runtime/helper source/binary/designated-requirement/required-blob/exact-suite hashes and ordered helper hashes; byte mutation is rejected. Keep all artifacts outside Git and protected root.

- [ ] **Step 3: Complete independent reviews and Task14 handoff (4 minutes).**

Specification review maps every design section to code/tests. Security review checks descriptor lifetime/rebind, code-sign identity, system flag/ACL behavior, root key layout, receipt-before-seal, no-replace, dynamic linker, key rotation/revocation, process/network confinement, and unchanged Task3. Both must be `APPROVED` with no P0/P1/P2. Each accepted finding gets a RED regression, smallest fix, affected GREEN gates, and an invariant-named commit before reviews repeat. Record exact reviewed commit, envelope SHA-256, all evidence hashes/reports/commands; this grants no installation, issuance, or custody action.

### Task 14: New exact-head privileged protected rollout, then later existing custody Task 15

**Files created only after new explicit approval naming Task14 commit and envelope SHA-256:**
- `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority/bootstrap/`
- `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority/control/`
- `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority/authorities/r4-disjoint-custody-v2-execution-v1/`
- failure only: one `quarantine/` child whose name is the fixed authority ID, a period, and the provisioner's generated lowercase UUID.

- [ ] **Step 1: Reapprove exact state and perform one Developer-ID bootstrap (5 minutes).**

Require fresh user approval of full Task14 commit and envelope SHA-256; changed HEAD/envelope/helper/candidate/key/receipt/revocation invalidates it. Then, only with reviewed Team ID/designated requirement:

~~~bash
reviewed_head="$(git rev-parse HEAD)"
evidence_root="/private/tmp/r4-protected-task14-${reviewed_head}"
approval_envelope="$evidence_root/task14-approval.json"
expected_approval_sha256="$(/usr/bin/awk '{print $1}' "$evidence_root/task14-approval.sha256")"
approved_helper="$evidence_root/provisioner"
candidate_directory="$evidence_root/candidate-input/candidate"
test -x "$approved_helper" && test -d "$candidate_directory" && test -f "$approval_envelope"
sudo "$approved_helper" bootstrap-install --approval-envelope "$approval_envelope" --expected-approval-sha256 "$expected_approval_sha256"
~~~

Expected: source/binary/requirement/envelope match, root generates and pins public key before receipt, seals private key only after sign probe, and reopens verifies bootstrap. A nonzero result is terminal retained state; never retry/delete/unseal.

- [ ] **Step 2: Provision once, then fresh-process verify (5 minutes).**

~~~bash
sudo '/Library/Application Support/mlb-walkforward-lab/protected-execution-authority/bootstrap/provisioner' provision --candidate-root "$candidate_directory"
sudo '/Library/Application Support/mlb-walkforward-lab/protected-execution-authority/bootstrap/provisioner' verify --authority-id r4-disjoint-custody-v2-execution-v1
~~~

Expected: provision validates/copies/seals once, and verify reports active non-revoked receipt SHA-256/public-key fingerprint. It does not issue R4 evidence, invoke custody, or authorize custody rollout.

- [ ] **Step 3: Gate existing custody Task 15 separately (2 minutes).**

Only after Step 2 succeeds, obtain new explicit approval for the current R4 recovery plan's Task 15. That approval must name its reviewed candidate HEAD plus the verified protected receipt SHA-256/public-key fingerprint. Task14 evidence/provisioning is never implicit custody approval.

## Spec coverage self-review

| Requirement | Tasks |
| --- | --- |
| Preserve and independently complete dirty fsmonitor/final-HEAD patch | 0 |
| Closed schemas/policy; exact runtime lock/URLs; tracked-HEAD offline candidate; exact Task14 envelope | 1, 2, 13 |
| Exact Developer ID SHA-1/Team ID signing, verification, and designated-requirement capture | 13, 14 |
| Swift skeleton/strict decoding/capability paths/bootstrap/root keys | 1, 3, 4 |
| Descriptor no-follow copy; link/type/path/collision rejection | 5 |
| Hermetic Python/Mach-O dependency closure | 6 |
| Receipt before seal/fsync/rename/quarantine | 7, 11 |
| Keyring/rotation/revocation and protected verification | 8, 9 |
| Original disjoint-plan Tasks 4–9 checkpoint; fixed issuer branch with legacy unaffected | checkpoint, 10 |
| Exact suite/Ruff/Swift/diff/scans/reviews/Task14 evidence | 12, 13 |
| New exact-head privileged ceremony before separate custody Task15 | 14 |

Self-check: the runtime lock binds each version, filename, URL, and digest before any offline extraction; the tracked-HEAD builder rechecks its held source closure; the approved Developer ID SHA-1 and Team ID are verified before the envelope hashes its binary and designated requirement; and the original Tasks 4–9 checkpoint is explicit before issuer integration. Later tasks reuse the declared names/types; code steps provide concrete snippets/commands/expected results; bootstrap public-key pin precedes receipt authority, private-key seal follows sign probe, receipt/signature fsync precedes seal/publish, and no alternate authority/root or implicit cleanup is introduced.

Plan complete and saved to `docs/superpowers/plans/2026-08-08-r4-protected-execution-authority.md`. Execute task-by-task with fresh workers or inline review checkpoints.
