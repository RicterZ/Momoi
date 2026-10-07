using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Momoi.Update;

/// <summary>One signed installation set. Never mix artifacts from separate checks.</summary>
public sealed record InstallCatalog(
    [property: JsonPropertyName("format_version")] int FormatVersion,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("runtime_id")] string RuntimeId,
    [property: JsonPropertyName("qq_pair_id")] string QQPairId,
    [property: JsonPropertyName("core")] UpdateArtifact Core,
    [property: JsonPropertyName("napcat")] UpdateArtifact NapCat,
    [property: JsonPropertyName("asr")] UpdateArtifact? ASR = null)
{
    public const string Url = "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/install.json";

    public static InstallCatalog Verify(byte[] envelope, byte[] key)
    {
        var value = JsonSerializer.Deserialize<InstallCatalog>(SignedLatest.VerifyPayload(envelope, key))
            ?? throw new InvalidDataException("安装清单为空。");
        if (value.FormatVersion != 1 || !Regex.IsMatch(value.Version ?? "", @"^\d+\.\d+\.\d+$") ||
            !Regex.IsMatch(value.RuntimeId ?? "", @"^[0-9a-f]{24}$") || !Regex.IsMatch(value.QQPairId ?? "", @"^[0-9a-f]{16}$"))
            throw new InvalidDataException("不支持的安装清单。");
        ValidateArtifact(value.Core, "core-installer");
        ValidateArtifact(value.NapCat, "napcat-installer");
        if (value.ASR is not null) ValidateArtifact(value.ASR, "asr-installer");
        if (value.Core.Version != value.Version || value.NapCat.Id != value.QQPairId)
            throw new InvalidDataException("安装组件版本不一致。");
        return value;
    }

    public static void ValidateArtifact(UpdateArtifact? artifact, string? expectedKind = null)
    {
        if (artifact is null || (artifact.Kind != "core-installer" && artifact.Kind != "napcat-installer" && artifact.Kind != "asr-installer") ||
            (expectedKind is not null && artifact.Kind != expectedKind) ||
            artifact.Size is < 1 or > 2L * 1024 * 1024 * 1024 ||
            !Regex.IsMatch(artifact.Id ?? "", @"^[A-Za-z0-9][A-Za-z0-9.-]{0,100}$") ||
            !Regex.IsMatch(artifact.Version ?? "", @"^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?$") ||
            !Regex.IsMatch(artifact.Sha256 ?? "", @"^[0-9a-f]{64}$") ||
            !Uri.TryCreate(artifact.Url, UriKind.Absolute, out var uri) || uri.Scheme != "https" ||
            uri.Host != new Uri(Url).Host || !uri.IsDefaultPort || uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0 ||
            !Regex.IsMatch(uri.AbsolutePath, artifact.Kind == "core-installer"
                ? @"^/windows/installers/Momoi-Setup-[A-Za-z0-9.-]+-x64\.exe$"
                : artifact.Kind == "asr-installer" ? @"^/windows/components/Momoi-ASR-Components-[A-Za-z0-9.-]+-x64\.exe$"
                : @"^/windows/components/Momoi-QQ-Components-[A-Za-z0-9.-]+-x64\.exe$"))
            throw new InvalidDataException("无效安装组件信息。");
    }

    public static async Task<byte[]> FetchEnvelopeAsync(CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(20));
        using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false });
        using var response = await client.GetAsync(Url + "?t=" + DateTimeOffset.UtcNow.ToUnixTimeSeconds(), HttpCompletionOption.ResponseHeadersRead, deadline.Token);
        response.EnsureSuccessStatusCode();
        await using var source = await response.Content.ReadAsStreamAsync(deadline.Token);
        using var buffer = new MemoryStream();
        var bytes = new byte[4096];
        int count;
        while ((count = await source.ReadAsync(bytes, deadline.Token)) > 0)
        {
            if (buffer.Length + count > 65536) throw new InvalidDataException("安装清单过大。");
            buffer.Write(bytes, 0, count);
        }
        byte[] envelope = buffer.ToArray();
        Verify(envelope, SignedLatest.EmbeddedPublicKey());
        return envelope;
    }
}
