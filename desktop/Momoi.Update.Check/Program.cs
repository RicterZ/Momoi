using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Momoi.Update;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

string root = Path.Combine(Path.GetTempPath(), "Momoi-update-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
try
{
    string install = Path.Combine(root, "install");
    string workspace = Path.Combine(root, "user");
    Directory.CreateDirectory(Path.Combine(install, "runtime"));
    File.WriteAllText(Path.Combine(install, "runtime/runtime.json"), "{\"runtime_id\":\"test-runtime\"}");
    string MakeArchive(string id, string runtime = "test-runtime", bool corrupt = false, string name = "app/backend_entry.py")
    {
        string archive = Path.Combine(root, id + ".zip");
        byte[] content = Encoding.UTF8.GetBytes("print('" + id + "')");
        string hash = Convert.ToHexStringLower(SHA256.HashData(content));
        var manifest = new ReleaseManifest(1, id, "1.0.0", runtime, new Dictionary<string, string> { [name] = hash });
        using var zip = ZipFile.Open(archive, ZipArchiveMode.Create);
        using (var writer = new StreamWriter(zip.CreateEntry("release.json").Open())) writer.Write(JsonSerializer.Serialize(manifest));
        using (var stream = zip.CreateEntry(name).Open()) stream.Write(corrupt ? Encoding.UTF8.GetBytes("tampered") : content);
        return archive;
    }
    string seed = MakeArchive("seed");
    ZipFile.ExtractToDirectory(seed, Path.Combine(install, "releases/bundled"));
    var store = new ReleaseStore(install, workspace);
    var original = store.Initialize();
    if (original.Manifest.ReleaseId != "seed") throw new Exception("Bundled initialization failed");
    var updates = new List<UpdateProgress>();
    var next = store.StageArchive(MakeArchive("next"), new RecordedProgress(updates));
    var extraction = updates.Where(value => value.Phase == "解压中").ToArray();
    if (extraction.Length < 2 || extraction[0].Completed != 0 ||
        extraction[^1].Completed != extraction[^1].Total ||
        updates[^1].Phase != "校验中") throw new Exception("Update extraction progress is incomplete");
    if (store.Initialize().Manifest.ReleaseId != "seed") throw new Exception("Staging changed the active release");
    store.Activate(next);
    if (store.Initialize().Manifest.ReleaseId != "next") throw new Exception("Activation failed");
    store.Activate(original);
    if (store.Initialize().Manifest.ReleaseId != "seed") throw new Exception("Rollback failed");
    foreach (string invalid in new[] { MakeArchive("corrupt", corrupt: true), MakeArchive("incompatible", runtime: "different-runtime"), MakeArchive("escape", name: "app/../../outside.py") })
    {
        bool rejected = false;
        try { store.StageArchive(invalid); }
        catch (InvalidDataException) { rejected = true; }
        if (!rejected || store.Initialize().Manifest.ReleaseId != "seed") throw new Exception("Invalid update was accepted");
    }
    if (Directory.GetFiles(root, "outside.py", SearchOption.AllDirectories).Length != 0) throw new Exception("ZIP traversal escaped");
    if (Directory.GetDirectories(Path.Combine(workspace, "releases"), ".stage-*").Length != 0) throw new Exception("Failed staging was not cleaned");
    var signingKey = new Ed25519PrivateKeyParameters(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32), 0);
    var signedPayload = new LatestRelease(1, "1.0.0", "next", "test-runtime", "https://example.com/code.zip", new string('a', 64), 100, 1);
    byte[] payload = JsonSerializer.SerializeToUtf8Bytes(signedPayload);
    var signer = new Ed25519Signer();
    signer.Init(true, signingKey);
    signer.BlockUpdate(payload, 0, payload.Length);
    byte[] signature = signer.GenerateSignature();
    byte[] Envelope(byte[] data, byte[] sig) => JsonSerializer.SerializeToUtf8Bytes(new { signed = Convert.ToBase64String(data), signature = Convert.ToBase64String(sig) });
    byte[] publicKey = signingKey.GeneratePublicKey().GetEncoded();
    if (SignedLatest.Verify(Envelope(payload, signature), publicKey).ReleaseId != "next") throw new Exception("Signature verification failed");
    payload[0] ^= 1;
    bool tampered = false;
    try { SignedLatest.Verify(Envelope(payload, signature), publicKey); }
    catch (InvalidDataException) { tampered = true; }
    if (!tampered) throw new Exception("Tampered signature accepted");
    if (SignedLatest.EmbeddedPublicKey().Length != 32) throw new Exception("Public key missing from assembly");
    byte[] SignCatalog(UpdateCatalog catalog)
    {
        byte[] raw = JsonSerializer.SerializeToUtf8Bytes(catalog);
        var sign = new Ed25519Signer();
        sign.Init(true, signingKey); sign.BlockUpdate(raw, 0, raw.Length);
        return Envelope(raw, sign.GenerateSignature());
    }
    var shellArtifact = new UpdateArtifact("1.1.3", "1.1.3", UpdateCatalog.Url.Replace("catalog.json", "shell/test.zip"), new string('a', 64), 100, "shell-zip");
    var pairArtifact = new UpdateArtifact("pair", "1.1.2", UpdateCatalog.Url.Replace("catalog.json", "components/test.exe"), new string('b', 64), 200, "napcat-installer");
    var catalog = new UpdateCatalog(1, "1.1.3", shellArtifact, pairArtifact);
    if (UpdateCatalog.Verify(SignCatalog(catalog), publicKey).Shell.Id != "1.1.3") throw new Exception("Catalog verification failed");
    foreach (var invalid in new[] {
        catalog with { FormatVersion = 2 },
        catalog with { Shell = shellArtifact with { Url = "https://untrusted.example/shell.zip" } },
        catalog with { Shell = shellArtifact with { Kind = "napcat-installer" } },
        catalog with { NapCat = pairArtifact with { Size = 0 } },
        catalog with { NapCat = pairArtifact with { Sha256 = "bad" } } })
    {
        bool rejected = false;
        try { UpdateCatalog.Verify(SignCatalog(invalid), publicKey); }
        catch (InvalidDataException) { rejected = true; }
        if (!rejected) throw new Exception("Invalid component catalog accepted");
    }
    byte[] brokenCatalog = SignCatalog(catalog);
    using (var document = JsonDocument.Parse(brokenCatalog))
    {
        var raw = Convert.FromBase64String(document.RootElement.GetProperty("signed").GetString()!);
        var sig = Convert.FromBase64String(document.RootElement.GetProperty("signature").GetString()!);
        sig[0] ^= 1;
        bool rejected = false;
        try { UpdateCatalog.Verify(Envelope(raw, sig), publicKey); }
        catch (InvalidDataException) { rejected = true; }
        if (!rejected) throw new Exception("Tampered catalog accepted");
    }
    if (args.Length > 0)
    {
        // Check Python cryptography signatures against the .NET implementation.
        var actual = SignedLatest.Verify(File.ReadAllBytes(args[0]), SignedLatest.EmbeddedPublicKey());
        Console.WriteLine("Verified Python-signed latest: " + actual.ReleaseId);
    }
    Console.WriteLine("PASS: staging, activation, rollback, hash validation, runtime mismatch and ZIP traversal rejection, Ed25519 verification and tamper rejection.");
}
finally { Directory.Delete(root, true); }

sealed class RecordedProgress(List<UpdateProgress> values) : IProgress<UpdateProgress>
{
    public void Report(UpdateProgress value) => values.Add(value);
}
