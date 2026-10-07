using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Momoi.Update;

public sealed record UpdateArtifact(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("url")] string Url,
    [property: JsonPropertyName("sha256")] string Sha256,
    [property: JsonPropertyName("size")] long Size,
    [property: JsonPropertyName("kind")] string Kind);
public sealed record UpdateCatalog(
    [property: JsonPropertyName("format_version")] int FormatVersion,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("shell")] UpdateArtifact Shell,
    [property: JsonPropertyName("napcat")] UpdateArtifact NapCat)
{
    public const string Url = "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/catalog.json";

    public static UpdateCatalog Verify(byte[] envelope, byte[] key)
    {
        var catalog = JsonSerializer.Deserialize<UpdateCatalog>(SignedLatest.VerifyPayload(envelope, key))
            ?? throw new InvalidDataException("更新清单为空。");
        if (catalog.FormatVersion != 1 || !Regex.IsMatch(catalog.Version ?? "", @"^\d+\.\d+\.\d+$"))
            throw new InvalidDataException("不支持的更新清单。");
        Validate(catalog.Shell, "shell-zip");
        Validate(catalog.NapCat, "napcat-installer");
        return catalog;
    }

    private static void Validate(UpdateArtifact? artifact, string kind)
    {
        if (artifact is null || artifact.Kind != kind || artifact.Size is < 1 or > 2L * 1024 * 1024 * 1024 ||
            !Regex.IsMatch(artifact.Id ?? "", @"^[A-Za-z0-9][A-Za-z0-9.-]{0,100}$") ||
            !Regex.IsMatch(artifact.Version ?? "", @"^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?$") ||
            !Regex.IsMatch(artifact.Sha256 ?? "", @"^[0-9a-f]{64}$") ||
            !Uri.TryCreate(artifact.Url, UriKind.Absolute, out var uri) || uri.Scheme != "https" ||
            uri.Host != new Uri(Url).Host || !string.IsNullOrEmpty(uri.UserInfo))
            throw new InvalidDataException("无效的组件更新信息。");
    }

    public static async Task<byte[]> FetchEnvelopeAsync(CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        token = deadline.Token;
        using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(15) };
        using var response = await client.GetAsync(Url + "?t=" + DateTimeOffset.UtcNow.ToUnixTimeSeconds(), HttpCompletionOption.ResponseHeadersRead, token);
        response.EnsureSuccessStatusCode();
        using var stream = await response.Content.ReadAsStreamAsync(token);
        using var buffer = new MemoryStream();
        var bytes = new byte[4096];
        int count;
        while ((count = await stream.ReadAsync(bytes, token)) > 0)
        {
            if (buffer.Length + count > 65536) throw new InvalidDataException("更新清单过大。");
            buffer.Write(bytes, 0, count);
        }
        Verify(buffer.ToArray(), SignedLatest.EmbeddedPublicKey());
        return buffer.ToArray();
    }

    public static async Task<string> DownloadAsync(UpdateArtifact artifact, string directory, IProgress<UpdateProgress> progress, CancellationToken token)
    {
        Validate(artifact, artifact.Kind == "shell-zip" ? "shell-zip" : "napcat-installer");
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, artifact.Id + (artifact.Kind == "shell-zip" ? ".zip" : ".exe"));
        string partial = path + ".partial";
        try
        {
            using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromMinutes(20) };
            using var response = await client.GetAsync(artifact.Url, HttpCompletionOption.ResponseHeadersRead, token);
            response.EnsureSuccessStatusCode();
            await using var stream = await response.Content.ReadAsStreamAsync(token);
            await using (var output = new FileStream(partial, FileMode.Create, FileAccess.Write, FileShare.None, 65536, true))
            {
                using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
                var bytes = new byte[65536];
                long size = 0; int count;
                while ((count = await stream.ReadAsync(bytes, token)) > 0)
                {
                    size += count;
                    if (size > artifact.Size) throw new InvalidDataException("下载大小超出签名清单。");
                    hash.AppendData(bytes, 0, count);
                    await output.WriteAsync(bytes.AsMemory(0, count), token);
                    progress.Report(new("下载中", size, artifact.Size));
                }
                if (size != artifact.Size || Convert.ToHexStringLower(hash.GetHashAndReset()) != artifact.Sha256)
                    throw new InvalidDataException("组件下载校验失败。");
            }
            File.Move(partial, path, true);
            return path;
        }
        finally { if (File.Exists(partial)) File.Delete(partial); }
    }
}
