using System;
using System.IO;
using System.Text.Json;

namespace Momoi.Desktop;

internal static class QQCallStatusFile
{
    // Status persistence is diagnostic; it must never stop a healthy voice host.
    internal static bool TryWrite(string path, string phase, string error, out Exception? failure)
    {
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(temporary, JsonSerializer.Serialize(new {
                phase, error, updated_at = DateTimeOffset.UtcNow.ToUnixTimeSeconds()
            }));
            File.Move(temporary, path, overwrite: true);
            failure = null;
            return true;
        }
        catch (Exception cause) when (cause is IOException or UnauthorizedAccessException)
        {
            failure = new IOException($"无法写入语音状态文件：{path}（临时文件：{temporary}）", cause);
            return false;
        }
        finally
        {
            try { File.Delete(temporary); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }
}
