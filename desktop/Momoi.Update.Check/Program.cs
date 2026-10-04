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
    var next = store.StageArchive(MakeArchive("next"));
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
    if (args.Length > 0)
    {
        // Check Python cryptography signatures against the .NET implementation.
        var actual = SignedLatest.Verify(File.ReadAllBytes(args[0]), SignedLatest.EmbeddedPublicKey());
        Console.WriteLine("Verified Python-signed latest: " + actual.ReleaseId);
    }
    Console.WriteLine("PASS: staging, activation, rollback, hash validation, runtime mismatch and ZIP traversal rejection, Ed25519 verification and tamper rejection.");
}
finally { Directory.Delete(root, true); }
