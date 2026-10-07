using System;
using System.Collections.Generic;
using System.Linq;

namespace Momoi.Desktop;

// Process readers never wait for the UI. Keep a bounded history for windows opened later.
internal static class LiveLog
{
    private const int MaxCharacters = 500_000;
    private static readonly object Gate = new();
    private static readonly Queue<Entry> Entries = new();
    private static long sequence;
    private static int characters;
    internal sealed record Entry(long Sequence, string Text);

    public static void Write(string source, string stream, string line)
    {
        if (line.Length > 8000) line = line[..8000] + " …[截断]";
        string text = $"{DateTimeOffset.Now:HH:mm:ss.fff} [{source}/{stream}] {line}{Environment.NewLine}";
        lock (Gate)
        {
            Entries.Enqueue(new Entry(++sequence, text));
            characters += text.Length;
            while (characters > MaxCharacters || Entries.Count > 3000)
                characters -= Entries.Dequeue().Text.Length;
        }
    }

    public static (long Latest, Entry[] Entries) ReadAfter(long after)
    {
        lock (Gate) return (sequence, Entries.Where(item => item.Sequence > after).ToArray());
    }
}
