using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;

namespace Momoi.Update;

/// <summary>Verified local-first, content-addressed downloads with resumable partials.</summary>
public sealed class ArtifactDownloader
{
    private readonly HttpClient client;
    public ArtifactDownloader(HttpClient client) { this.client = client; }

    public static async Task<bool> MatchesAsync(string path, UpdateArtifact artifact, CancellationToken token)
    {
        if (!File.Exists(path) || new FileInfo(path).Length != artifact.Size) return false;
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 65536, true);
        return Convert.ToHexStringLower(await SHA256.HashDataAsync(stream, token)) == artifact.Sha256;
    }

    public async Task<string> DownloadAsync(UpdateArtifact artifact, string cache, IEnumerable<string> localDirectories,
        IProgress<UpdateProgress> progress, CancellationToken token)
    {
        InstallCatalog.ValidateArtifact(artifact);
        string name = Path.GetFileName(new Uri(artifact.Url).AbsolutePath);
        string directory = Path.Combine(cache, artifact.Sha256);
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, name);
        // Never execute a mutable local candidate directly: copy it into the verified cache.
        string[] localNames = artifact.Kind == "core-installer" ? [name, $"Momoi-Setup-{artifact.Version}-x64.exe"] : [name];
        foreach (string local in localDirectories.SelectMany(folder => localNames.Select(file => Path.Combine(folder, file))).Prepend(path).Distinct())
        {
            if (!await MatchesAsync(local, artifact, token)) continue;
            if (local != path)
            {
                progress.Report(new("校验本地组件", artifact.Size, artifact.Size));
                File.Copy(local, path, true);
                if (!await MatchesAsync(path, artifact, token)) { File.Delete(path); continue; }
            }
            progress.Report(new("使用已校验组件", artifact.Size, artifact.Size));
            return path;
        }
        if (File.Exists(path)) File.Delete(path);
        string partial = path + ".partial";
        Exception? last = null;
        for (int attempt = 0; attempt < 3; attempt++)
        {
            token.ThrowIfCancellationRequested();
            try
            {
                long offset = File.Exists(partial) ? new FileInfo(partial).Length : 0;
                if (offset > artifact.Size) { File.Delete(partial); offset = 0; }
                if (offset < artifact.Size)
                {
                    using var request = new HttpRequestMessage(HttpMethod.Get, artifact.Url);
                    if (offset > 0) request.Headers.Range = new RangeHeaderValue(offset, null);
                    using var headersDeadline = CancellationTokenSource.CreateLinkedTokenSource(token);
                    headersDeadline.CancelAfter(TimeSpan.FromSeconds(30));
                    using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, headersDeadline.Token);
                    response.EnsureSuccessStatusCode();
                    if (response.StatusCode == HttpStatusCode.PartialContent)
                    {
                        var range = response.Content.Headers.ContentRange;
                        if (range is null || range.Unit != "bytes" || range.From != offset || range.Length != artifact.Size || range.To != artifact.Size - 1)
                            throw new InvalidDataException("下载续传范围与签名清单不符。");
                    }
                    else if (response.StatusCode == HttpStatusCode.OK) offset = 0;
                    else throw new InvalidDataException("服务器返回了非预期下载响应。");
                    if (response.Content.Headers.ContentLength is long length && length != artifact.Size - offset)
                        throw new InvalidDataException("下载长度与签名清单不符。");
                    await using var source = await response.Content.ReadAsStreamAsync(token);
                    await using var target = new FileStream(partial, offset > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None, 65536, true);
                    var bytes = new byte[65536];
                    progress.Report(new(offset > 0 ? "继续下载" : "下载中", offset, artifact.Size));
                    while (true)
                    {
                        using var idleDeadline = CancellationTokenSource.CreateLinkedTokenSource(token);
                        idleDeadline.CancelAfter(TimeSpan.FromSeconds(30));
                        int count = await source.ReadAsync(bytes, idleDeadline.Token);
                        if (count == 0) break;
                        offset += count;
                        if (offset > artifact.Size) throw new InvalidDataException("下载大小超出签名清单。");
                        await target.WriteAsync(bytes.AsMemory(0, count), token);
                        progress.Report(new("下载中", offset, artifact.Size));
                    }
                }
                if (!File.Exists(partial) || new FileInfo(partial).Length < artifact.Size) throw new EndOfStreamException("下载连接提前结束。");
                progress.Report(new("校验中", 0, artifact.Size));
                if (!await MatchesAsync(partial, artifact, token)) throw new InvalidDataException("安装组件 SHA-256 或大小校验失败。");
                File.Move(partial, path, true);
                return path;
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { throw; }
            catch (Exception error) when (error is IOException or InvalidDataException or HttpRequestException or OperationCanceledException)
            {
                last = error;
                if (error is InvalidDataException && File.Exists(partial)) File.Delete(partial);
                if (attempt == 2) break;
                progress.Report(new("连接中断，正在重试", 0, artifact.Size));
                await Task.Delay(TimeSpan.FromSeconds(attempt + 1), token);
            }
        }
        throw new IOException("组件下载未完成，可重试；已下载的有效部分保留。", last);
    }
}
