// Program.cs
// .NET 8 console app built as a DLL (UseAppHost=false)
// Simplified: no TIFF stage, no image preprocessing. Directly OCR PDFs.
// No 'using' statements—everything disposed via try/finally with DisposeQuietly().
// Drop the built DLL into the same folder as Atalasoft 11.5 DLLs + license files.
// Run with: dotnet OcrBatchDemo.dll

using System;
using System.IO;
using System.Linq;
using System.Diagnostics;
using System.Globalization;
using System.Collections.Generic;

using Atalasoft.Imaging;
using Atalasoft.Imaging.Codec;
using Atalasoft.Imaging.Codec.Pdf;
using Atalasoft.Ocr;
using Atalasoft.Ocr.OmniPage;

internal static class Program
{
    // ***** EDIT THESE *****
    private const string INPUT_DIR  = @"D:\ocr\in";
    private const string OUTPUT_DIR = @"D:\ocr\out";
    private const string OMNIPAGE_RESOURCES = @"D:\ocr\omnipage_resources";
    private const string OCR_LANGUAGE = "en-US";
    private const int    PDF_RASTER_DPI = 300;     // rasterization DPI for PDFs (good default)
    private const bool   EMIT_LAYOUT_JSON = true;  // requires WebControls OCR translator present

    private static int Main(string[] args)
    {
        if (!Directory.Exists(INPUT_DIR))  { Console.Error.WriteLine($"Input missing: {INPUT_DIR}"); return 2; }
        Directory.CreateDirectory(OUTPUT_DIR);
        if (!Directory.Exists(OMNIPAGE_RESOURCES)) { Console.Error.WriteLine($"OmniPage resources missing: {OMNIPAGE_RESOURCES}"); return 3; }

        // Make sure the PDF decoder rasterizes at a known DPI.
        EnsurePdfDecoderWithDpi(PDF_RASTER_DPI);

        var pdfs = Directory.EnumerateFiles(INPUT_DIR, "*.pdf", SearchOption.TopDirectoryOnly)
                            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                            .ToList();

        if (pdfs.Count == 0) { Console.WriteLine("No PDFs found."); return 0; }

        Console.WriteLine($"Input : {INPUT_DIR}");
        Console.WriteLine($"Output: {OUTPUT_DIR}");
        Console.WriteLine($"Omni  : {OMNIPAGE_RESOURCES}");
        Console.WriteLine($"Lang  : {OCR_LANGUAGE}");
        Console.WriteLine($"DPI   : {PDF_RASTER_DPI}");
        Console.WriteLine();

        foreach (var pdf in pdfs)
        {
            try { RunJobFor(pdf); }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"FAIL: {Path.GetFileName(pdf)} -> {ex.GetType().Name}: {ex.Message}");
            }
        }

        Console.WriteLine("\nDone.");
        return 0;
    }

    private static void RunJobFor(string pdfPath)
    {
        var name   = Path.GetFileNameWithoutExtension(pdfPath);
        var outDir = Path.Combine(OUTPUT_DIR, name);
        Directory.CreateDirectory(outDir);

        var logPath = Path.Combine(outDir, "ocr_metrics.log");
        StreamWriter log = null;

        try
        {
            log = new StreamWriter(logPath, append: false);
            Log(log, $"== OCR Job: {name} ==");
            Log(log, $"Started   : {DateTimeOffset.Now:O}");
            Log(log, $"Input PDF : {pdfPath}");
            Log(log, $"Output Dir: {outDir}");
            Log(log, "");

            var overall = Stopwatch.StartNew();

            // DIRECT OCR (no TIFF stage)
            string searchablePdf = Path.Combine(outDir, $"{name}.searchable.pdf");
            string plaintext     = Path.Combine(outDir, $"{name}.txt");
            string layoutJson    = Path.Combine(outDir, $"{name}.layout.json");

            OmniPageLoader loader = null;
            OmniPageEngine engine = null;
            ImageSource images = null;          // FileSystemImageSource over the PDF
            PdfTranslator pdfTranslator = null;
            TextTranslator textTranslator = null;

            var perPage = new List<(int page, double ms)>();
            int pageCount = 0;

            try
            {
                // Set up OCR engine + resources
                loader = new OmniPageLoader(OMNIPAGE_RESOURCES);
                engine = new OmniPageEngine();
                engine.RecognitionCultures = new[] { new CultureInfo(OCR_LANGUAGE) };

                // Multi-page source directly from the PDF
                images = new FileSystemImageSource(new[] { pdfPath }, true);
                pageCount = images.TotalImages;

                // 1) Searchable PDF with per-page timing
                var swPdf = Stopwatch.StartNew();
                pdfTranslator = new PdfTranslator();

                // Page timing using PageConstructing
                var pageTimer = new Stopwatch();
                int current = -1;

                // Attach handler, but keep reference so we can detach safely
                EventHandler<PageEventArgs> handler = (s, e) =>
                {
                    if (pageTimer.IsRunning)
                    {
                        pageTimer.Stop();
                        perPage.Add((current + 1, pageTimer.Elapsed.TotalMilliseconds));
                    }
                    current = e.PageIndex;
                    pageTimer.Restart();
                };

                try
                {
                    pdfTranslator.PageConstructing += handler;
                    engine.Translate(images, "application/pdf", searchablePdf, pdfTranslator);
                    if (pageTimer.IsRunning)
                    {
                        pageTimer.Stop();
                        perPage.Add((current + 1, pageTimer.Elapsed.TotalMilliseconds));
                    }
                }
                finally
                {
                    pdfTranslator.PageConstructing -= handler;
                }
                swPdf.Stop();
                Log(log, $"OCR->PDF : {swPdf.Elapsed.TotalMilliseconds:n0} ms");

                // 2) Plain text
                var swTxt = Stopwatch.StartNew();
                textTranslator = new TextTranslator();
                engine.Translate(images, "text/plain", plaintext, textTranslator);
                swTxt.Stop();
                Log(log, $"OCR->Text: {swTxt.Elapsed.TotalMilliseconds:n0} ms");

                // 3) Optional layout JSON
                if (EMIT_LAYOUT_JSON)
                {
                    var swJson = Stopwatch.StartNew();
                    TryJsonLayout(engine, images, layoutJson);
                    swJson.Stop();
                    Log(log, $"OCR->JSON: {swJson.Elapsed.TotalMilliseconds:n0} ms (optional)");
                }
            }
            finally
            {
                DisposeQuietly(textTranslator);
                DisposeQuietly(pdfTranslator);
                DisposeQuietly(images);   // safe even if not IDisposable
                DisposeQuietly(engine);
                DisposeQuietly(loader);
            }

            overall.Stop();
            Log(log, $"Overall   : {overall.Elapsed.TotalMilliseconds:n0} ms");
            Log(log, "");

            Log(log, $"Pages     : {pageCount}");
            Log(log, "Per-page timings (PDF translation):");
            foreach (var p in perPage) Log(log, $"  Page {p.page:000}: {p.ms:n0} ms");

            Console.WriteLine($"OK: {name}");
        }
        finally
        {
            DisposeQuietly(log);
        }
    }

    private static void EnsurePdfDecoderWithDpi(int dpi)
    {
        // Register or update PdfDecoder’s Resolution so the PDF rasterizes at the desired DPI.
        for (int i = 0; i < RegisteredDecoders.Decoders.Count; i++)
        {
            var existing = RegisteredDecoders.Decoders[i] as PdfDecoder;
            if (existing != null) { existing.Resolution = dpi; return; }
        }
        RegisteredDecoders.Decoders.Add(new PdfDecoder { Resolution = dpi });
    }

    private static void TryJsonLayout(OcrEngine engine, ImageSource images, string outPath)
    {
        try
        {
            // Optional JSON layout translator (if WebControls OCR assembly is present)
            var t = Type.GetType("Atalasoft.Imaging.WebControls.OCR.JsonTranslator, Atalasoft.dotImage.WebControls", throwOnError: false);
            if (t == null) return;

            ITranslator translator = null;
            try
            {
                translator = (ITranslator)Activator.CreateInstance(t);
                engine.Translate(images, "application/json", outPath, translator);
            }
            finally
            {
                DisposeQuietly(translator);
            }
        }
        catch
        {
            // optional; ignore if not available
        }
    }

    private static void Log(StreamWriter log, string message)
    {
        if (log != null) log.WriteLine(message);
    }

    private static void DisposeQuietly(object obj)
    {
        try { (obj as IDisposable)?.Dispose(); } catch { }
    }
}