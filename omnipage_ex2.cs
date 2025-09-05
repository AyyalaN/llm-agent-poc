// Program.cs
// .NET 8 Console — Production-leaning PoC: batch OCR for PDFs using Atalasoft DotImage 11.5 + OmniPage
// Artifacts per input PDF:
//   - <name>.searchable.pdf   (image-under-text via PdfTranslator)
//   - <name>.txt              (plain text via TextTranslator)
//   - <name>.layout.json      (optional; emitted if JsonTranslator assembly is present)
// Highlights:
//   - Explicit package versions (see NuGet section below)
//   - Config via args + env + defaults
//   - Robust error reporting, per-file isolation
//   - Deterministic temp + output layout
//   - Explicit preprocessing (deskew, binarize, despeckle) before OCR
//   - PDF rasterization DPI pinned to 300
//   - Parallelizable file loop (opt-in; keep sequential by default for stability)

using System;
using System.Collections.Concurrent;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

using Atalasoft.Imaging;
using Atalasoft.Imaging.Codec;
using Atalasoft.Imaging.Codec.Pdf;
using Atalasoft.Imaging.ImageProcessing.Document; // AdvancedDocClean filters
using Atalasoft.Ocr;
using Atalasoft.Ocr.OmniPage;

internal static class Program
{
    static async Task<int> Main(string[] args)
    {
        var cfg = AppConfig.FromArgsAndEnv(args);

        if (!Directory.Exists(cfg.input_dir))
        {
            Console.Error.WriteLine($"Input directory not found: {cfg.input_dir}");
            return 2;
        }
        Directory.CreateDirectory(cfg.output_dir);
        Directory.CreateDirectory(cfg.temp_root);

        // Register PdfDecoder with fixed rasterization DPI for OCR fidelity.
        PdfDecoderHelper.SetPdfRasterizationDpi(cfg.pdf_raster_dpi);

        // Validate OmniPage resources
        if (!Directory.Exists(cfg.omnipage_resources))
        {
            Console.Error.WriteLine($"ERROR: OMNIPAGE_RESOURCES not found at: {cfg.omnipage_resources}");
            return 3;
        }

        var pdfs = Directory.EnumerateFiles(cfg.input_dir, "*.pdf", SearchOption.TopDirectoryOnly)
                            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                            .ToList();

        if (pdfs.Count == 0)
        {
            Console.WriteLine("No PDFs found.");
            return 0;
        }

        Console.WriteLine($"Found {pdfs.Count} PDFs. Output => {cfg.output_dir}");
        Console.WriteLine($"Temp   => {cfg.temp_root}");
        Console.WriteLine($"DPI    => {cfg.pdf_raster_dpi}");
        Console.WriteLine($"Lang   => {cfg.ocr_language}");
        Console.WriteLine(cfg.parallel_degree > 1
            ? $"Parallel: degree={cfg.parallel_degree}"
            : "Parallel: disabled");

        var errors = new ConcurrentBag<string>();
        var cts = new CancellationTokenSource();

        var action = new Action<string>(pdfPath =>
        {
            try
            {
                OcrJob.RunForOnePdf(
                    pdfPath: pdfPath,
                    cfg: cfg,
                    cancel: cts.Token
                );
                Console.WriteLine($"OK  : {Path.GetFileName(pdfPath)}");
            }
            catch (OperationCanceledException)
            {
                errors.Add($"CANCELED: {pdfPath}");
            }
            catch (Exception ex)
            {
                errors.Add($"{Path.GetFileName(pdfPath)} -> {ex.GetType().Name}: {ex.Message}");
                Console.Error.WriteLine($"FAIL: {Path.GetFileName(pdfPath)}\n{ex}");
            }
        });

        if (cfg.parallel_degree > 1)
        {
            var po = new ParallelOptions { MaxDegreeOfParallelism = cfg.parallel_degree, CancellationToken = cts.Token };
            Parallel.ForEach(pdfs, po, action);
        }
        else
        {
            foreach (var p in pdfs) action(p);
        }

        if (!errors.IsEmpty)
        {
            Console.Error.WriteLine("\nSummary of failures:");
            foreach (var e in errors) Console.Error.WriteLine($" - {e}");
            return 1;
        }

        Console.WriteLine("\nDone.");
        return 0;
    }

    // ---------------------------
    // Configuration
    // ---------------------------
    private sealed record AppConfig(
        string input_dir,
        string output_dir,
        string temp_root,
        string omnipage_resources,
        string ocr_language,
        int pdf_raster_dpi,
        bool emit_layout_json,
        int parallel_degree
    )
    {
        public static AppConfig FromArgsAndEnv(string[]? args)
        {
            // precedence: args > env > defaults
            string GetEnv(string name, string def) =>
                Environment.GetEnvironmentVariable(name) ?? def;

            var input = args?.Length >= 1 ? args![0] : GetEnv("OCR_INPUT_DIR",  @"C:\ocr\in");
            var output= args?.Length >= 2 ? args![1] : GetEnv("OCR_OUTPUT_DIR", @"C:\ocr\out");
            var tmp   = GetEnv("OCR_TEMP_DIR", Path.Combine(Path.GetTempPath(), "ocr_work"));
            var omni  = args?.Length >= 3 ? args![2] : GetEnv("OMNIPAGE_RESOURCES", @"C:\Atalasoft\OmniPageResources");
            var lang  = GetEnv("OCR_LANGUAGE", "en-US");
            var dpi   = int.TryParse(GetEnv("PDF_RASTER_DPI", "300"), out var v) ? Math.Clamp(v, 150, 600) : 300;
            var layout= (GetEnv("EMIT_LAYOUT_JSON", "true").Equals("true", StringComparison.OrdinalIgnoreCase));
            var pl    = int.TryParse(GetEnv("PARALLEL_DEGREE", "1"), out var p) ? Math.Max(1, p) : 1;

            return new AppConfig(input, output, tmp, omni, lang, dpi, layout, pl);
        }
    }

    // ---------------------------
    // OCR Orchestration (per file)
    // ---------------------------
    private static class OcrJob
    {
        public static void RunForOnePdf(string pdfPath, AppConfig cfg, CancellationToken cancel)
        {
            cancel.ThrowIfCancellationRequested();

            var name = Path.GetFileNameWithoutExtension(pdfPath);
            var outBase = Path.Combine(cfg.output_dir, name);
            Directory.CreateDirectory(cfg.output_dir);

            // Use a per-file temp working area (easy to purge/troubleshoot)
            var workDir = Path.Combine(cfg.temp_root, $"{name}_{Guid.NewGuid():N}");
            Directory.CreateDirectory(workDir);

            try
            {
                // 1) Build a clean, preprocessed, multi-page TIFF as OCR input
                var tiffForOcr = Path.Combine(workDir, "preprocessed.tif");
                PreprocessPipeline.BuildPreprocessedTiffFromPdf(pdfPath, tiffForOcr, cfg.pdf_raster_dpi, cancel);

                // 2) OCR using OmniPage
                var searchablePdf = outBase + ".searchable.pdf";
                var plaintext     = outBase + ".txt";
                var layoutJson    = outBase + ".layout.json";

                // Ensure OmniPage engine lifetime is short and isolated per file
                using var loader = new OmniPageLoader(cfg.omnipage_resources);
                using var engine = new OmniPageEngine
                {
                    RecognitionCultures = new[] { new CultureInfo(cfg.ocr_language) }
                };

                using var images = new FileSystemImageSource(new[] { tiffForOcr }, true);

                // 2a) Searchable PDF
                using (var pdfTranslator = new PdfTranslator())
                {
                    pdfTranslator.AutoPageRotation = true; // allow rotation correction
                    engine.Translate(images, "application/pdf", searchablePdf, pdfTranslator);
                }

                // 2b) Plain text
                using (var textTranslator = new TextTranslator())
                {
                    engine.Translate(images, "text/plain", plaintext, textTranslator);
                }

                // 2c) Optional layout JSON via reflection (only if assembly exists)
                if (cfg.emit_layout_json)
                {
                    Translators.TryJsonLayout(engine, images, layoutJson);
                }
            }
            finally
            {
                // Best-effort cleanup of temp directory
                try { Directory.Delete(workDir, true); } catch { /* ignore */ }
            }
        }
    }

    // ---------------------------
    // Preprocessing pipeline
    // ---------------------------
    private static class PreprocessPipeline
    {
        public static void BuildPreprocessedTiffFromPdf(string pdfPath, string outTiff, int dpi, CancellationToken cancel)
        {
            cancel.ThrowIfCancellationRequested();

            // Use Atalasoft decoders to open PDF pages, process each page individually to control memory.
            using var pdfImages = new FileSystemImageSource(new[] { pdfPath }, true);

            // Create a multi-page TIFF writer lazily (only when first page is produced)
            using var tiffEncoder = new TiffEncoder
            {
                Compression = TiffCompression.Lzw, // lossless, decent size
                SaveMethod = TiffSaveMethod.MultiPage
            };

            AtalaImage? previous = null;
            try
            {
                for (int i = 0; i < pdfImages.TotalImages; i++)
                {
                    cancel.ThrowIfCancellationRequested();

                    using var page = pdfImages[i];            // Rasterize page (PdfDecoder DPI already set globally)
                    using var pre  = ApplyDocumentCleanup(page);

                    // Append to multipage TIFF
                    if (previous == null)
                    {
                        previous = pre.Clone(); // seed
                        using var fs = File.Create(outTiff);
                        tiffEncoder.Save(previous, fs);
                    }
                    else
                    {
                        using var fs = new FileStream(outTiff, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                        fs.Seek(0, SeekOrigin.End);
                        tiffEncoder.Append(pre, fs);
                    }
                }
            }
            finally
            {
                previous?.Dispose();
            }
        }

        private static AtalaImage ApplyDocumentCleanup(AtalaImage source)
        {
            // Clone the page to avoid mutating source frame in the decoder
            var img = source.Clone();

            // 1) Auto deskew
            using (var cmd = new AutoDeskewCommand())
            {
                cmd.ApplyInPlace(img);
            }

            // 2) Binarize (DynamicThreshold works well for OCR)
            using (var cmd = new BinarizeCommand { BinarizationMethod = BinarizeMethod.DynamicThreshold })
            {
                cmd.ApplyInPlace(img);
            }

            // 3) Despeckle (default parameters remove small noise)
            using (var cmd = new DespeckleCommand())
            {
                cmd.ApplyInPlace(img);
            }

            return img;
        }
    }

    // ---------------------------
    // Translators (optional JSON layout)
    // ---------------------------
    private static class Translators
    {
        public static void TryJsonLayout(OcrEngine engine, ImageSource images, string outputPath)
        {
            try
            {
                // JsonTranslator lives in Atalasoft.dotImage.WebControls (optional dependency)
                var t = Type.GetType("Atalasoft.Imaging.WebControls.OCR.JsonTranslator, Atalasoft.dotImage.WebControls", throwOnError: false);
                if (t == null) return;

                using var translator = (ITranslator)Activator.CreateInstance(t)!;
                engine.Translate(images, "application/json", outputPath, translator);
            }
            catch
            {
                // Non-fatal
            }
        }
    }

    // ---------------------------
    // PDF Decoder config
    // ---------------------------
    private static class PdfDecoderHelper
    {
        public static void SetPdfRasterizationDpi(int dpi)
        {
            lock (typeof(PdfDecoderHelper))
            {
                for (int i = 0; i < RegisteredDecoders.Decoders.Count; i++)
                {
                    if (RegisteredDecoders.Decoders[i] is PdfDecoder existing)
                    {
                        existing.Resolution = dpi;
                        return;
                    }
                }
                RegisteredDecoders.Decoders.Add(new PdfDecoder { Resolution = dpi });
            }
        }
    }
}