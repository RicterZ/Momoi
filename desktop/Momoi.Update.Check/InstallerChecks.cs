using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using Momoi.Update;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

internal static class InstallerChecks
{
    internal static async Task RunAsync(string root)
    {
        byte[] data = RandomNumberGenerator.GetBytes(220_000);
        string hash = Convert.ToHexStringLower(SHA256.HashData(data));
        var core = new UpdateArtifact("1.1.3-test", "1.1.3", "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/installers/Momoi-Setup-1.1.3-test-x64.exe", hash, data.Length, "core-installer");
        var qq = new UpdateArtifact("0123456789abcdef", "1.1.2", "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/components/Momoi-QQ-Components-1.1.2-test-x64.exe", hash, data.Length, "napcat-installer");
        var asr = new UpdateArtifact("asr-test", "1.1.3", "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/components/Momoi-ASR-Components-1.1.3-test-x64.exe", hash, data.Length, "asr-installer");
        var catalog = new InstallCatalog(1, "1.1.3", new string('a', 24), qq.Id, core, qq, asr);
        var key = new Ed25519PrivateKeyParameters(RandomNumberGenerator.GetBytes(32), 0);
        byte[] Envelope(InstallCatalog value)
        {
            byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(value);
            var signer = new Ed25519Signer(); signer.Init(true, key); signer.BlockUpdate(bytes, 0, bytes.Length);
            return JsonSerializer.SerializeToUtf8Bytes(new { signed = Convert.ToBase64String(bytes), signature = Convert.ToBase64String(signer.GenerateSignature()) });
        }
        byte[] publicKey = key.GeneratePublicKey().GetEncoded();
        if (InstallCatalog.Verify(Envelope(catalog), publicKey).Core != core) throw new Exception("Installation signature verification failed");
        foreach (var invalid in new[] {
            catalog with { Core = core with { Url = "https://example.com/windows/installers/Momoi-Setup-1.1.3-x64.exe" } },
            catalog with { Core = core with { Url = core.Url + "?redirect=1" } },
            catalog with { Core = core with { Kind = "napcat-installer" } },
            catalog with { Core = core with { Version = "1.1.4" } },
            catalog with { Core = core with { Size = long.MaxValue } },
            catalog with { QQPairId = new string('b', 16) },
            catalog with { RuntimeId = "unknown" },
            catalog with { ASR = asr with { Url = "https://example.com/asr.exe" } },
            catalog with { ASR = asr with { Kind = "core-installer" } }
        })
        {
            bool rejected = false; try { InstallCatalog.Verify(Envelope(invalid), publicKey); } catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new Exception("Unsafe installation set was accepted");
        }
        var progress = new RecordedProgress(new List<UpdateProgress>());
        // A dropped response is resumed at the exact byte offset with a fresh request.
        var resumed = new ScriptedHandler(data, dropFirst: true);
        using (var client = new HttpClient(resumed))
        {
            string folder = Path.Combine(root, "resumed");
            string path = await new ArtifactDownloader(client).DownloadAsync(core, folder, [], progress, CancellationToken.None);
            if (!await ArtifactDownloader.MatchesAsync(path, core, CancellationToken.None) || !resumed.Offsets.SequenceEqual(new long[] { 0, 70001 })) throw new Exception("Truncated response did not resume correctly");
            await new ArtifactDownloader(client).DownloadAsync(core, folder, [], progress, CancellationToken.None);
            if (resumed.Offsets.Count != 2) throw new Exception("Verified cache was redownloaded");
        }
        // Servers ignoring Range must restart, not append a full response to partial bytes.
        var ignores = new ScriptedHandler(data, ignoreRange: true);
        using (var client = new HttpClient(ignores))
        {
            string folder = Path.Combine(root, "ignored-range");
            string partial = Partial(folder, core); Directory.CreateDirectory(Path.GetDirectoryName(partial)!); File.WriteAllBytes(partial, data[..70001]);
            string path = await new ArtifactDownloader(client).DownloadAsync(core, folder, [], progress, CancellationToken.None);
            if (!await ArtifactDownloader.MatchesAsync(path, core, CancellationToken.None)) throw new Exception("Ignored range corrupted the download");
        }
        // A same-sized but corrupt completed partial must be discarded, never launched.
        using (var client = new HttpClient(new ScriptedHandler(data)))
        {
            string folder = Path.Combine(root, "corrupt-partial"); string partial = Partial(folder, core);
            Directory.CreateDirectory(Path.GetDirectoryName(partial)!); File.WriteAllBytes(partial, new byte[data.Length]);
            string path = await new ArtifactDownloader(client).DownloadAsync(core, folder, [], progress, CancellationToken.None);
            if (!await ArtifactDownloader.MatchesAsync(path, core, CancellationToken.None)) throw new Exception("Corrupt partial was accepted");
        }
        // An offline ZIP uses its normal core filename; only signed hashes establish identity.
        string local = Path.Combine(root, "offline"); Directory.CreateDirectory(local);
        File.WriteAllBytes(Path.Combine(local, "Momoi-Setup-1.1.3-x64.exe"), data);
        var localOnly = new ScriptedHandler(data);
        using (var client = new HttpClient(localOnly))
        {
            string path = await new ArtifactDownloader(client).DownloadAsync(core, Path.Combine(root, "local-cache"), [local], progress, CancellationToken.None);
            if (localOnly.Offsets.Count != 0 || path.StartsWith(local + Path.DirectorySeparatorChar)) throw new Exception("Local-first installation did not copy into verified cache");
        }
        var wrongRange = new ScriptedHandler(data, wrongRange: true);
        using (var client = new HttpClient(wrongRange))
        {
            bool rejected = false;
            try { await new ArtifactDownloader(client).DownloadAsync(core, Path.Combine(root, "wrong-range"), [], progress, CancellationToken.None); }
            catch (IOException) { rejected = true; }
            if (!rejected) throw new Exception("Malformed Content-Range was accepted");
        }
        using (var canceled = new CancellationTokenSource())
        using (var client = new HttpClient(new ScriptedHandler(data)))
        {
            string folder = Path.Combine(root, "cancel"); string partial = Partial(folder, core);
            Directory.CreateDirectory(Path.GetDirectoryName(partial)!); File.WriteAllBytes(partial, data[..70001]); canceled.Cancel();
            try { await new ArtifactDownloader(client).DownloadAsync(core, folder, [], progress, canceled.Token); throw new Exception("Cancellation ignored"); }
            catch (OperationCanceledException) { }
            if (new FileInfo(partial).Length != 70001) throw new Exception("Cancellation discarded resumable partials");
        }
        Console.WriteLine("PASS: signed installation sets, wrong-origin rejection, resumed downloads, ignored ranges, corrupt files, local-first cache and cancellation.");
    }
    private static string Partial(string folder, UpdateArtifact artifact) => Path.Combine(folder, artifact.Sha256, Path.GetFileName(new Uri(artifact.Url).AbsolutePath) + ".partial");
    private sealed class ScriptedHandler(byte[] data, bool dropFirst = false, bool ignoreRange = false, bool wrongRange = false) : HttpMessageHandler
    {
        public List<long> Offsets { get; } = [];
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            long offset = request.Headers.Range?.Ranges.Single().From ?? 0; Offsets.Add(offset);
            bool partial = (offset > 0 && !ignoreRange) || wrongRange;
            byte[] body = dropFirst && Offsets.Count == 1 ? data[..70001] : data[(int)(ignoreRange ? 0 : offset)..];
            var response = new HttpResponseMessage(partial ? HttpStatusCode.PartialContent : HttpStatusCode.OK) { Content = new ByteArrayContent(body) };
            response.Content.Headers.ContentLength = data.Length - (ignoreRange ? 0 : offset);
            if (partial) response.Content.Headers.ContentRange = new ContentRangeHeaderValue(offset + (wrongRange ? 1 : 0), data.Length - 1, data.Length);
            return Task.FromResult(response);
        }
    }
}
