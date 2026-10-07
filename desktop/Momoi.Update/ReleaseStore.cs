using System.IO.Compression;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Momoi.Update;

public sealed record ReleaseManifest(
    [property: JsonPropertyName("format_version")] int FormatVersion,
    [property: JsonPropertyName("release_id")] string ReleaseId,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("runtime_id")] string RuntimeId,
    [property: JsonPropertyName("files")] Dictionary<string, string> Files);
public sealed record CodeRelease(string Directory, ReleaseManifest Manifest);
public sealed record UpdateProgress(string Phase, long Completed = 0, long Total = 0);

/// <summary>Immutable version directories and an atomic current-version pointer.</summary>
public sealed class ReleaseStore
{
    private const long MaxZipBytes = 64 * 1024 * 1024;
    private const long MaxExtractedBytes = 128 * 1024 * 1024;
    private readonly string installDirectory, workspace, releases, pointer, runtimeId;
    private static readonly Regex SafeId = new(@"^[A-Za-z0-9][A-Za-z0-9.-]{0,100}$", RegexOptions.CultureInvariant);

    public ReleaseStore(string installDirectory, string workspace)
    {
        this.installDirectory = installDirectory;
        this.workspace = workspace;
        releases = Path.Combine(workspace, "releases");
        pointer = Path.Combine(workspace, "active-release.json");
        using var runtime = JsonDocument.Parse(File.ReadAllText(Path.Combine(installDirectory, "runtime", "runtime.json")));
        runtimeId = runtime.RootElement.GetProperty("runtime_id").GetString()!;
        System.IO.Directory.CreateDirectory(releases);
    }

    public CodeRelease Initialize()
    {
        string bundled = Path.Combine(installDirectory, "releases", "bundled");
        var seed = Validate(bundled);
        string destination = Path.Combine(releases, seed.Manifest.ReleaseId);
        if (!System.IO.Directory.Exists(destination))
        {
            string staging = Path.Combine(releases, ".seed-" + Guid.NewGuid().ToString("N"));
            try
            {
                CopyDirectory(bundled, staging);
                Validate(staging);
                System.IO.Directory.Move(staging, destination);
            }
            finally { if (System.IO.Directory.Exists(staging)) System.IO.Directory.Delete(staging, true); }
        }
        if (File.Exists(pointer))
        {
            try
            {
                using var current = JsonDocument.Parse(File.ReadAllText(pointer));
                string id = current.RootElement.GetProperty("release_id").GetString()!;
                if (!SafeId.IsMatch(id)) throw new InvalidDataException("Invalid release pointer");
                return Validate(Path.Combine(releases, id));
            }
            catch (Exception error) when (error is IOException or JsonException or KeyNotFoundException or InvalidDataException)
            {
                // A component upgrade can invalidate an older runtime-bound release.
                File.Move(pointer, pointer + ".previous", overwrite: true);
            }
        }
        var release = Validate(destination);
        Activate(release);
        return release;
    }

    public void Activate(CodeRelease release)
    {
        if (!SafeId.IsMatch(release.Manifest.ReleaseId) ||
            Path.GetFullPath(release.Directory) != Path.GetFullPath(Path.Combine(releases, release.Manifest.ReleaseId)))
            throw new InvalidDataException("Release is outside the managed directory");
        Validate(release.Directory);
        string temporary = pointer + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { release_id = release.Manifest.ReleaseId }));
            File.Move(temporary, pointer, overwrite: true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    public async Task<CodeRelease> DownloadAsync(LatestRelease latest, CancellationToken cancellationToken, IProgress<UpdateProgress>? progress = null)
    {
        var url = new Uri(latest.Url);
        if (latest.RuntimeId != runtimeId) throw new InvalidDataException("此更新需要新的运行组件，请安装新版安装包。");
        if (url.Scheme != "https") throw new InvalidDataException("Update URL must use HTTPS");
        string archivePath = Path.Combine(releases, ".download-" + Guid.NewGuid().ToString("N") + ".zip");
        string staging = Path.Combine(releases, ".stage-" + Guid.NewGuid().ToString("N"));
        try
        {
            progress?.Report(new("下载中", 0, latest.Size));
            using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromMinutes(5) };
            using var response = await client.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
            response.EnsureSuccessStatusCode();
            if (response.Content.Headers.ContentLength > MaxZipBytes) throw new InvalidDataException("Update archive is too large");
            await using (var destination = File.Create(archivePath))
            await using (var source = await response.Content.ReadAsStreamAsync(cancellationToken))
            {
                var buffer = new byte[81920];
                long length = 0;
                int count;
                while ((count = await source.ReadAsync(buffer, cancellationToken)) > 0)
                {
                    length += count;
                    if (length > MaxZipBytes) throw new InvalidDataException("Update archive is too large");
                    await destination.WriteAsync(buffer.AsMemory(0, count), cancellationToken);
                    progress?.Report(new("下载中", length, latest.Size));
                }
            }
            progress?.Report(new("校验中"));
            using (var downloaded = File.OpenRead(archivePath))
            {
                if (downloaded.Length != latest.Size || Convert.ToHexStringLower(SHA256.HashData(downloaded)) != latest.Sha256)
                    throw new InvalidDataException("更新 ZIP 与已签名的清单不符。");
            }
            var release = await Task.Run(() => StageArchive(archivePath, staging, progress, cancellationToken), cancellationToken);
            if (release.Manifest.ReleaseId != latest.ReleaseId || release.Manifest.Version != latest.Version || release.Manifest.RuntimeId != latest.RuntimeId)
                throw new InvalidDataException("Update release identity mismatch");
            return release;
        }
        finally
        {
            if (File.Exists(archivePath)) File.Delete(archivePath);
            if (System.IO.Directory.Exists(staging)) System.IO.Directory.Delete(staging, true);
        }
    }

    // Public for offline validation and deterministic update tests.
    public CodeRelease StageArchive(string archivePath, IProgress<UpdateProgress>? progress = null) => StageArchive(archivePath,
        Path.Combine(releases, ".stage-" + Guid.NewGuid().ToString("N")), progress);

    private CodeRelease StageArchive(string archivePath, string staging, IProgress<UpdateProgress>? progress = null, CancellationToken cancellationToken = default)
    {
        try
        {
            using var archive = ZipFile.OpenRead(archivePath);
            if (archive.Entries.Count > 20_000) throw new InvalidDataException("Too many update files");
            var manifestEntry = archive.GetEntry("release.json") ?? throw new InvalidDataException("Missing release manifest");
            if (manifestEntry.Length > 4 * 1024 * 1024) throw new InvalidDataException("Manifest too large");
            ReleaseManifest manifest;
            using (var stream = manifestEntry.Open())
                manifest = JsonSerializer.Deserialize<ReleaseManifest>(stream) ?? throw new InvalidDataException("Invalid manifest");
            ValidateManifest(manifest);
            System.IO.Directory.CreateDirectory(staging);
            long expanded = 0;
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            long extracted = 0;
            progress?.Report(new("解压中", 0, archive.Entries.Count));
            foreach (var entry in archive.Entries)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (string.IsNullOrEmpty(entry.Name)) continue;
                string name = entry.FullName;
                if (!names.Add(name)) throw new InvalidDataException("Duplicate update path");
                if (name != "release.json" && !manifest.Files.ContainsKey(name)) throw new InvalidDataException("Unlisted update file");
                if (name != "release.json") ValidateRelativePath(name);
                if (((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000) throw new InvalidDataException("Symbolic links are unsupported");
                expanded += entry.Length;
                if (expanded > MaxExtractedBytes) throw new InvalidDataException("Expanded update is too large");
                string path = Path.Combine(staging, name.Replace('/', Path.DirectorySeparatorChar));
                System.IO.Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                entry.ExtractToFile(path);
                progress?.Report(new("解压中", ++extracted, archive.Entries.Count));
            }
            progress?.Report(new("校验中"));
            var validated = Validate(staging);
            string destination = Path.Combine(releases, validated.Manifest.ReleaseId);
            if (System.IO.Directory.Exists(destination)) return Validate(destination);
            System.IO.Directory.Move(staging, destination);
            return new CodeRelease(destination, validated.Manifest);
        }
        finally { if (System.IO.Directory.Exists(staging)) System.IO.Directory.Delete(staging, true); }
    }

    private CodeRelease Validate(string directory)
    {
        var manifest = JsonSerializer.Deserialize<ReleaseManifest>(File.ReadAllText(Path.Combine(directory, "release.json")))
            ?? throw new InvalidDataException("Invalid release manifest");
        ValidateManifest(manifest);
        foreach (var (name, expected) in manifest.Files)
        {
            ValidateRelativePath(name);
            string file = Path.Combine(directory, name.Replace('/', Path.DirectorySeparatorChar));
            using var stream = File.OpenRead(file);
            string actual = Convert.ToHexStringLower(SHA256.HashData(stream));
            if (!string.Equals(actual, expected, StringComparison.Ordinal)) throw new InvalidDataException("Update checksum mismatch: " + name);
        }
        return new CodeRelease(directory, manifest);
    }

    private void ValidateManifest(ReleaseManifest manifest)
    {
        if (manifest.FormatVersion != 1 || manifest.ReleaseId is null || !SafeId.IsMatch(manifest.ReleaseId) || manifest.Files is null || manifest.Files.Count is < 1 or > 20_000)
            throw new InvalidDataException("Unsupported release manifest");
        if (manifest.RuntimeId != runtimeId)
            throw new InvalidDataException("此更新需要新的运行组件，请安装新版安装包。");
        if (!manifest.Files.ContainsKey("app/backend_entry.py")) throw new InvalidDataException("Missing backend entry");
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (name, hash) in manifest.Files)
        {
            ValidateRelativePath(name);
            if (!names.Add(name) || hash is null || !Regex.IsMatch(hash, @"^[0-9a-f]{64}$")) throw new InvalidDataException("Invalid manifest file");
        }
    }

    private static void ValidateRelativePath(string name)
    {
        if (!name.StartsWith("app/", StringComparison.Ordinal) || name.Contains('\\') || name.Contains(':') || name.Split('/').Any(part => part is "" or "." or ".." || part.EndsWith('.') || part.EndsWith(' ')))
            throw new InvalidDataException("Unsafe update path");
    }

    private static void CopyDirectory(string source, string destination)
    {
        System.IO.Directory.CreateDirectory(destination);
        foreach (string file in System.IO.Directory.GetFiles(source)) File.Copy(file, Path.Combine(destination, Path.GetFileName(file)));
        foreach (string directory in System.IO.Directory.GetDirectories(source)) CopyDirectory(directory, Path.Combine(destination, Path.GetFileName(directory)));
    }
}
