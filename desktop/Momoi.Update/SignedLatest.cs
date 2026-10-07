using System.Net.Http;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

namespace Momoi.Update;

public sealed record LatestRelease(
    [property: JsonPropertyName("format_version")] int FormatVersion,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("release_id")] string ReleaseId,
    [property: JsonPropertyName("runtime_id")] string RuntimeId,
    [property: JsonPropertyName("url")] string Url,
    [property: JsonPropertyName("sha256")] string Sha256,
    [property: JsonPropertyName("size")] long Size,
    [property: JsonPropertyName("published_at")] long PublishedAt);

public static class SignedLatest
{
    private const int MaxManifestBytes = 64 * 1024;

    public static byte[] EmbeddedPublicKey()
    {
        using var stream = typeof(SignedLatest).Assembly.GetManifestResourceStream("Momoi.Update.PublicKey")
            ?? throw new InvalidDataException("Missing embedded update public key");
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        byte[] key = buffer.ToArray();
        if (key.Length != 32) throw new InvalidDataException("Invalid embedded Ed25519 public key");
        return key;
    }

    public static async Task<LatestRelease> FetchAsync(CancellationToken cancellationToken)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        cancellationToken = deadline.Token;
        if (!Uri.TryCreate(UpdateSource.LatestManifestUrl, UriKind.Absolute, out var uri) || uri.Scheme != "https")
            throw new InvalidOperationException("此构建尚未内置 COS 发布地址，需要在发布前补齐外壳地址常量。");
        using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(30) };
        using var request = new HttpRequestMessage(HttpMethod.Get, uri);
        request.Headers.CacheControl = new System.Net.Http.Headers.CacheControlHeaderValue { NoCache = true };
        using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
        response.EnsureSuccessStatusCode();
        if (response.Content.Headers.ContentLength > MaxManifestBytes) throw new InvalidDataException("Latest manifest too large");
        await using var source = await response.Content.ReadAsStreamAsync(cancellationToken);
        using var buffer = new MemoryStream();
        var bytes = new byte[4096];
        int read;
        while ((read = await source.ReadAsync(bytes, cancellationToken)) > 0)
        {
            if (buffer.Length + read > MaxManifestBytes) throw new InvalidDataException("Latest manifest too large");
            buffer.Write(bytes, 0, read);
        }
        return Verify(buffer.ToArray(), EmbeddedPublicKey());
    }

    public static byte[] VerifyPayload(byte[] envelope, byte[] publicKey)
    {
        if (envelope.Length > MaxManifestBytes || publicKey.Length != 32) throw new InvalidDataException("Invalid signed manifest");
        try
        {
            using var document = JsonDocument.Parse(envelope);
            byte[] payload = Convert.FromBase64String(document.RootElement.GetProperty("signed").GetString()!);
            byte[] signature = Convert.FromBase64String(document.RootElement.GetProperty("signature").GetString()!);
            var verifier = new Ed25519Signer();
            verifier.Init(false, new Ed25519PublicKeyParameters(publicKey, 0));
            verifier.BlockUpdate(payload, 0, payload.Length);
            if (signature.Length != 64 || !verifier.VerifySignature(signature)) throw new InvalidDataException("更新签名验证失败。");
            return payload;
        }
        catch (Exception error) when (error is JsonException or FormatException or KeyNotFoundException or ArgumentException or InvalidOperationException)
        { throw new InvalidDataException("Invalid signed manifest", error); }
    }

    public static LatestRelease Verify(byte[] envelope, byte[] publicKey)
    {
        if (envelope.Length > MaxManifestBytes || publicKey.Length != 32) throw new InvalidDataException("Invalid signed manifest");
        try
        {
            byte[] payload = VerifyPayload(envelope, publicKey);
            var latest = JsonSerializer.Deserialize<LatestRelease>(payload) ?? throw new InvalidDataException("Invalid latest payload");
            if (latest.FormatVersion != 1 || latest.Size is < 1 or > 64 * 1024 * 1024 || latest.PublishedAt <= 0 ||
                latest.Sha256 is null || !Regex.IsMatch(latest.Sha256, @"^[0-9a-f]{64}$") ||
                latest.Version is null || !Regex.IsMatch(latest.Version, @"^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?$") ||
                latest.ReleaseId is null || !Regex.IsMatch(latest.ReleaseId, @"^[A-Za-z0-9][A-Za-z0-9.-]{0,100}$") ||
                !Uri.TryCreate(latest.Url, UriKind.Absolute, out var url) || url.Scheme != "https" || !string.IsNullOrEmpty(url.UserInfo))
                throw new InvalidDataException("Invalid signed latest fields");
            return latest;
        }
        catch (Exception error) when (error is JsonException or FormatException or KeyNotFoundException or ArgumentException or InvalidOperationException)
        { throw new InvalidDataException("Invalid signed latest manifest", error); }
    }
}
