// Program.cs
// .NET 8 console app built as a DLL (UseAppHost=false)
// NO 'using' statements — only try/catch/finally with DisposeQuietly.
// Hardcode your paths below, then run from the folder containing Atalasoft DLLs + licenses.

using System;
using System.IO;
using System.Linq;
using System.Diagnostics;
using System.Globalization;
using System.Collections.Generic;

using Atalasoft.Imaging;
using Atalasoft.Imaging.Codec;
using Atalasoft.Imaging.Codec.Pdf;
using Atalasoft.Imaging.ImageProcessing.Document; // AdvancedDocClean filters
using Atalasoft.Ocr;
using Atalasoft.Ocr.OmniPage;

internal static class Program
{
    // ***** EDIT THESE *****
    private const string INPUT_DIR  = @"D:\ocr\in";
    private const string OUTPUT_DIR = @"D:\ocr\out";
    private const string OMNIPAGE_RESOURCES = @"D:\ocr\omnipage_resources";
    private const string OCR_LANGUAGE = "en-US";   // e.g., "en-US", "es-ES"
    private const int    PDF_RASTER_DPI = 300;     // typical OCR DPI
    private const bool   EMIT_LAYOUT_JSON = true;  // requires WebControls OCR translator present

    private static int Main(string[] args)
    {
        if (!Directory.Exists(INPUT_DIR))  { Console.Error.WriteLine($"Input missing: {INPUT_DIR}"); return 2; }
        Directory.CreateDirectory(OUTPUT_DIR);
        if (!Directory.Exists(OMNIPAGE_RESOURCES)) { Console.Error.WriteLine($"OmniPage resources missing: {OMNIPAGE_RESOURCES}"); return 3; }

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

            // 1) Preprocess -> multipage TIFF (kept in output folder for inspection)
            var swPre = Stopwatch.StartNew();
            var tiffForOcr = Path.Combine(outDir, "_preprocessed.tif");
            int pageCount  = BuildPreprocessedTiffFromPdf(pdfPath, tiffForOcr);
            swPre.Stop();
            Log(log, $"Preprocess: {swPre.Elapsed.TotalMilliseconds:n0} ms (pages: {pageCount})");

            // 2) OCR with OmniPage
            string searchablePdf = Path.Combine(outDir, $"{name}.searchable.pdf");
            string plaintext     = Path.Combine(outDir, $"{name}.txt");
            string layoutJson    = Path.Combine(outDir, $"{name}.layout.json");

            OmniPageLoader loader = null;
            OmniPageEngine engine = null;
            ImageSource images = null;
            PdfTranslator pdfTranslator = null;
            TextTranslator textTranslator = null;

            var perPage = new List<(int page, double ms)>();

            try
            {
                loader = new OmniPageLoader(OMNIPAGE_RESOURCES);
                engine = new OmniPageEngine();
                engine.RecognitionCultures = new[] { new CultureInfo(OCR_LANGUAGE) };

                // Multipage image source for the preprocessed TIFF
                images = new FileSystemImageSource(new[] { tiffForOcr }, true);

                // 2a) Searchable PDF with per-page timing
                var swPdf = Stopwatch.StartNew();
                pdfTranslator = new PdfTranslator();

                var pageTimer = new Stopwatch();
                int current = -1;
                try
                {
                    pdfTranslator.PageConstructing += (s, e) =>
                    {
                        if (pageTimer.IsRunning)
                        {
                            pageTimer.Stop();
                            perPage.Add((current + 1, pageTimer.Elapsed.TotalMilliseconds));
                        }
                        current = e.PageIndex;
                        pageTimer.Restart();
                    };

                    engine.Translate(images, "application/pdf", searchablePdf, pdfTranslator);
                    if (pageTimer.IsRunning)
                    {
                        pageTimer.Stop();
                        perPage.Add((current + 1, pageTimer.Elapsed.TotalMilliseconds));
                    }
                }
                finally
                {
                    // detach handlers defensively
                    pdfTranslator.PageConstructing -= (s, e) => { };
                }
                swPdf.Stop();
                Log(log, $"OCR->PDF : {swPdf.Elapsed.TotalMilliseconds:n0} ms");

                // 2b) Plain text
                var swTxt = Stopwatch.StartNew();
                textTranslator = new TextTranslator();
                engine.Translate(images, "text/plain", plaintext, textTranslator);
                swTxt.Stop();
                Log(log, $"OCR->Text: {swTxt.Elapsed.TotalMilliseconds:n0} ms");

                // 2c) Optional layout JSON
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
                // FileSystemImageSource may or may not be IDisposable depending on build; harmless if not.
                DisposeQuietly(images);
                DisposeQuietly(engine);
                DisposeQuietly(loader);
            }

            overall.Stop();
            Log(log, $"Overall   : {overall.Elapsed.TotalMilliseconds:n0} ms");
            Log(log, "");
            Log(log, "Per-page timings (PDF translation):");
            foreach (var p in perPage) Log(log, $"  Page {p.page:000}: {p.ms:n0} ms");

            Console.WriteLine($"OK: {name}");
        }
        finally
        {
            DisposeQuietly(log);
        }
    }

    private static void Log(StreamWriter log, string message)
    {
        if (log != null) log.WriteLine(message);
    }

    private static void EnsurePdfDecoderWithDpi(int dpi)
    {
        // Register or update PdfDecoder’s Resolution so PDF rasterizes at OCR-friendly DPI.
        for (int i = 0; i < RegisteredDecoders.Decoders.Count; i++)
        {
            var existing = RegisteredDecoders.Decoders[i] as PdfDecoder;
            if (existing != null) { existing.Resolution = dpi; return; }
        }
        RegisteredDecoders.Decoders.Add(new PdfDecoder { Resolution = dpi });
    }

    private static int BuildPreprocessedTiffFromPdf(string pdfPath, string outTiff)
    {
        int pageCount = 0;
        ImageSource src = null;
        TiffEncoder tiffEncoder = null;
        AtalaImage first = null;

        try
        {
            // Multi-page input
            src = new FileSystemImageSource(new[] { pdfPath }, true);

            tiffEncoder = new TiffEncoder
            {
                Compression = TiffCompression.Lzw,
                SaveMethod  = TiffSaveMethod.MultiPage
            };

            for (int i = 0; i < src.TotalImages; i++)
            {
                AtalaImage page = null;
                AtalaImage cleaned = null;
                FileStream fs = null;

                try
                {
                    page = src[i];
                    cleaned = ApplyCleanup(page);

                    if (pageCount == 0)
                    {
                        first = cleaned.Clone();
                        fs = File.Create(outTiff);
                        tiffEncoder.Save(first, fs);
                    }
                    else
                    {
                        fs = new FileStream(outTiff, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                        fs.Seek(0, SeekOrigin.End);
                        tiffEncoder.Append(cleaned, fs);
                    }
                    pageCount++;
                }
                finally
                {
                    DisposeQuietly(fs);
                    DisposeQuietly(cleaned);
                    DisposeQuietly(page);
                }
            }
        }
        finally
        {
            DisposeQuietly(first);
            DisposeQuietly(tiffEncoder);
            // FileSystemImageSource may not be IDisposable in some builds — this is safe.
            DisposeQuietly(src);
        }

        return pageCount;
    }

    private static AtalaImage ApplyCleanup(AtalaImage source)
    {
        // Commands typically implement IDisposable; we guard-dispose via finally.
        AtalaImage img = source.Clone();

        AutoDeskewCommand deskew = null;
        BinarizeCommand bin = null;
        DespeckleCommand despeckle = null;

        try
        {
            deskew = new AutoDeskewCommand();
            deskew.ApplyInPlace(img);

            bin = new BinarizeCommand { BinarizationMethod = BinarizeMethod.DynamicThreshold };
            bin.ApplyInPlace(img);

            despeckle = new DespeckleCommand();
            despeckle.ApplyInPlace(img);

            return img;
        }
        catch
        {
            DisposeQuietly(img);
            throw;
        }
        finally
        {
            DisposeQuietly(despeckle);
            DisposeQuietly(bin);
            DisposeQuietly(deskew);
        }
    }

    private static void TryJsonLayout(OcrEngine engine, ImageSource images, string outPath)
    {
        try
        {
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

    private static void DisposeQuietly(object obj)
    {
        try
        {
            (obj as IDisposable)?.Dispose();
        }
        catch
        {
            // swallow
        }
    }
}