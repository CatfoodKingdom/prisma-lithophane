// Generated directly from SETTINGS_DRAWER_SCRIPT.md. Keep learner-facing copy in sync.
export const SETTINGS_DRAWER_COPY = Object.freeze({
  "settings-drawer.intro": {
    "title": "Settings shape every stage of a solve",
    "body": "Prisma turns an image into a lithophane in stages. The Settings Drawer is organized roughly in the order those stages happen, from the shape of the print itself through to the white surface you view through.\n\n[IMPROVE INTRODUCTION, ADD CONTEXT, MAYBE A FIGURE?]\n\nThis guide surveys every setting in the Settings Drawer. For each setting, it introduces what it does, how it can change the result, and when it is useful. The goal is to build intuition for how these settings interact and work together to shape the finished lithophane."
  },
  "settings-drawer.enable-advanced": {
    "title": "Open Settings and turn on Advanced",
    "body": "To continue, open the **Settings** drawer and toggle display of **Advanced** settings on."
  },
  "settings-drawer.chapters": {
    "title": "Choose a section",
    "body": "Select a topic to learn more about its settings. When you are done, select Finish to exit this guide."
  },
  "settings-drawer.essentials.intro": {
    "title": "Essentials",
    "body": "The settings in Essentials define the problem Prisma is trying to solve and the constraints under which it must solve it. Solve Mode chooses the approach Prisma will use to reconstruct the image. The remaining settings specify the size of the lithophane’s building blocks, the white material used to construct it, and the physical space available for its base, color layers, and cap.\n\nTogether, these choices determine which layer arrangements are possible and shape every result Prisma can produce. The rest of the Settings Drawer works within the foundation established here."
  },
  "settings-drawer.essentials.stack": {
    "title": "What you are building",
    "body": "[DO THIS LATER]\n\nAdd improved diagram.\n\nThe diagram shows the stack: a white base, the color layers, and the white cap you view through.\n\nThe line below the table shows the maximum number of color layers available with the current thickness settings. A recipe may use fewer."
  },
  "settings-drawer.essentials.solve-mode": {
    "title": "Solve Mode",
    "body": "**Solve Mode** chooses between two fundamentally different ways to use the colored layers and white cap to reconstruct an image.\n\n- **Color** primarily reconstructs the image using the layers of colored filament. The white cap plays a secondary, limited role in completing the final appearance.\n\n- **Luminance** gives the white cap a larger, explicit role in reproducing the image's brightness and fine detail."
  },
  "settings-drawer.essentials.solve-mode-choice": {
    "title": "Choosing a Solve Mode",
    "body": "**Color** is a better default choice for most images and palettes.\n\nConsider using **Luminance** when complex shading and or finely-detailed, highly contrasting texture is important to an image's composition. It can also be used if you specifically want more visible texture to be present in the white cap."
  },
  "settings-drawer.essentials.solve-pitch": {
    "title": "Solve Pitch",
    "body": "**Solve Pitch** is the physical size of the square \"pixel\" building blocks Prisma uses to plan a lithophane.\n\nIt sets both the resolution of the image used for the solve and the resolution visible in the finished lithophane. A smaller value produces a more detailed lithophane than a larger one.\n\nPrisma builds Solve Pitch from the active Extrusion Width. Use **−** and **+** to choose a whole-number multiple of that width."
  },
  "settings-drawer.essentials.solve-pitch-matching": {
    "title": "Solve Pitch and Extrusion Width",
    "body": "The smallest Solve Pitch equals the active Extrusion Width. Each larger choice adds one more Extrusion Width, so every available pitch remains printable on the selected solve grid.\n\nChoose a smaller multiple for more detail or a larger multiple for a coarser, faster solve. Printer Configuration determines which Extrusion Widths are available for each nozzle."
  },
  "settings-drawer.essentials.max-total-thickness": {
    "title": "Max Thickness",
    "body": "**Max Thickness** sets the height limit for the base, colored layers, and white cap in the image area. It defines the total vertical space Prisma may use to reconstruct the image.\n\nIncreasing it gives Prisma room for more possible layer arrangements, but also permits a thicker lithophane that takes longer to print."
  },
  "settings-drawer.essentials.thickness-budget": {
    "title": "How the thickness is allocated",
    "body": "Prisma uses these settings to determine how many colored layers fit:\n\n**Maximum number of colored layers = [(Max Thickness − Base Thickness) ÷ Layer Height] − Min Cap Layers**\n\nPrisma cannot use partial layers. Max Thickness minus Base Thickness must be a whole-number multiple of Layer Height."
  },
  "settings-drawer.essentials.layer-height": {
    "title": "Layer Height",
    "body": "**Layer Height** sets the physical height of each printed layer. Choose a value within the range supported by your active nozzle, then use that exact value in your slicer.\n\nChoose a supported Layer Height for each Image. Images defined by subtle variations in shading can benefit from smaller values."
  },
  "settings-drawer.essentials.layer-height-tradeoff": {
    "title": "What Layer Height costs",
    "body": "Smaller Layer Heights give Prisma finer control over filament thickness and can reproduce subtler colors. They can also increase print time and filament waste."
  },
  "settings-drawer.essentials.white-filament": {
    "title": "Base/Cap Filament",
    "body": "**Base/Cap Filament** prints the base and cap. It is not typically changed from print to print.\n\nSelect the exact white filament you will use to print. It should have a sufficiently high transmission distance to reproduce very light colors. A low transmission distance can limit the lithophane's light-to-dark range."
  },
  "settings-drawer.essentials.base-thickness": {
    "title": "Base Thickness",
    "body": "The white base slightly diffuses incoming light, helps the first layer print successfully, and provides a solid foundation for everything printed above it.\n\nSet **Base Thickness** to the first-layer height you use in your slicer so the entire base prints as one layer. Common values are **0.2 mm** for a 0.4 mm nozzle and **0.12–0.15 mm** for a 0.2 mm nozzle."
  },
  "settings-drawer.essentials.min-cap-layers": {
    "title": "Min Cap Layers",
    "body": "Every color stack is covered by a continuous white **boundary cap**. **Min Cap Layers** is the thinnest it may be anywhere. Its physical thickness is this count multiplied by **Layer Height**.\n\nThe default is two layers. More layers can soften harsh boundaries and calm dark or strongly colored areas, but leave less height for color and detail. One layer preserves the most room for saturation and contrast."
  },
  "settings-drawer.preprocessing.intro": {
    "title": "Preprocessing",
    "body": "Preprocessing further modifies your adjusted image before the solver begins turning it into blueprints for a 3D model.\n\nYour original file and your **Image** settings are untouched. Everything in this section changes only what the solver sees."
  },
  "settings-drawer.preprocessing.resample-kernel": {
    "title": "Resample kernel",
    "body": "**Resample kernel** controls how your photo is scaled to fit the solve grid. **Lanczos** keeps edges crisper. **Area** averages over each grid cell for a softer result.\n\nThe difference is usually subtle; use whichever method you prefer."
  },
  "settings-drawer.preprocessing.order": {
    "title": "The order modules run in",
    "body": "The enabled modules run in a fixed order:\n\n**Noise Reduction → Print-Scale Smoothing → Flat-Area Smoothing → Palette Tone Fit → Palette Saturation Fit**\n\nEach module receives the result of the one before it. Their effects can overlap, so a combination may not look like two separate changes simply added together."
  },
  "settings-drawer.preprocessing.noise-reduction": {
    "title": "Noise Reduction",
    "body": "**Noise Reduction** smooths small variations in color and brightness while preserving stronger edges. Larger presets blend across bigger differences and over a wider area.\n\nUse it when a surface that should look even breaks into many small patches. Start with **Light**; stronger settings can also erase wanted texture or create new color transitions.\n\nA subtle change may fix a small problem area without noticeably changing the rest of the image."
  },
  "settings-drawer.preprocessing.print-scale-smoothing": {
    "title": "Print-Scale Smoothing",
    "body": "**Print-Scale Smoothing** blends small color variations in an image at a scale proportional to your active Extrusion Width, producing broader, simpler color areas. Stronger presets affect a larger area or repeat the smoothing.\n\nTry it when color keeps shifting across a surface that should look calm. Changing Extrusion Width can change its effect even when the preset stays the same."
  },
  "settings-drawer.preprocessing.flat-area-smoothing": {
    "title": "Flat-Area Smoothing",
    "body": "**Flat-Area Smoothing** pushes gradual variation into flatter areas, so the image resolves into broader shapes. At higher strengths it takes on a painted, poster-like look.\n\nMost presets flatten brightness while leaving color transitions in place. **Graphic** also flattens color; that is what separates it from **Bold**.\n\nThis is mainly an aesthetic choice. The simpler shapes may help the image or remove detail you wanted to keep."
  },
  "settings-drawer.preprocessing.palette-tone-fit": {
    "title": "Palette Tone Fit",
    "body": "**Palette Tone Fit** redistributes brightness toward the darkest and lightest tones your current Palette and settings can reproduce.\n\nTry it when shadows go flat black or highlights wash out to blank white. Stronger presets apply more of the remapping to the whole image, so midtones may move as well.\n\nIts result changes with your Palette and settings. When **Palette Saturation Fit** is also on, that module acts on these already-adjusted tones."
  },
  "settings-drawer.preprocessing.palette-saturation-fit": {
    "title": "Palette Saturation Fit",
    "body": "**Palette Saturation Fit** reduces colors that are too vivid for your current Palette and settings while preserving their hue.\n\nIt begins easing colors before they reach the limit instead of stopping at a hard edge. **Compression start** controls how early that begins; **Compression softness** controls how gradually it ramps up.\n\nIts effect is often subtle, but it can also flatten color variation that looked fine already."
  },
  "settings-drawer.solver.intro": {
    "title": "Color Solver",
    "body": "The Color Solver decides which filaments go where. It divides the image into color areas, works out a recipe for each one, then refines the result.\n\nThese settings change how it searches for a recipe, and how those areas are shaped."
  },
  "settings-drawer.solver.appearance-model": {
    "title": "Appearance Model",
    "body": "The **Appearance Model** predicts how layered filaments will look when the lithophane is backlit. Prisma uses that prediction for recipe search and the preview.\n\n**Color Model v2** is the newer default and is normally the best choice. **Color Model v1** is the older model, kept mainly for comparison. Changing the model can change the whole solve, not just one color-matching step."
  },
  "settings-drawer.solver.white-point-rescale": {
    "title": "White-point rescale",
    "body": "**White-point rescale** shifts the solve so the brightest white the current base and cap can make becomes the image's white. It can make pale or gray areas look more consistent, but often darkens or tints the whole preview.\n\nLeave it off unless light areas look blotchy or carry unwanted color, then try turning it on. **Palette Tone Fit** acts earlier, so using both can compound brightness changes. Changing the white filament or its base and minimum-cap thickness can also change the rescale."
  },
  "settings-drawer.solver.max-colors": {
    "title": "Max colors per region",
    "body": "**Max colors per region** limits how many colored filaments may be layered in one region's recipe.\n\nHigher values search more combinations and can take much longer to solve. Three is a useful baseline; higher values may not improve the result. The extra colors also need enough height to fit, so thickness settings can change how useful a higher value is."
  },
  "settings-drawer.solver.mismatch-tolerance": {
    "title": "Color mismatch tolerance",
    "body": "Not every photo color can be printed with every Palette. **Color mismatch tolerance** decides how readily Prisma adjusts hard-to-print colors before matching recipes.\n\nLower values adjust more; higher values leave more targets unchanged."
  },
  "settings-drawer.solver.out-of-gamut": {
    "title": "Out-of-gamut handling",
    "body": "**Nearest reachable color** favors the closest overall printable color, which may shift the hue. **Preserve hue** protects the hue by reducing vividness and, when needed, brightness.\n\nNeither is always better. The choice is often between a hue shift and a less vivid color."
  },
  "settings-drawer.solver.chroma-weight": {
    "title": "Chroma weight",
    "body": "**Chroma weight** changes how Prisma judges recipe matches. Toward **Color**, it protects hue and vividness while allowing brightness to drift. Toward **Tone**, it protects the light-and-dark pattern while allowing color to drift.\n\nIt affects recipe selection after any out-of-gamut adjustment; it does not decide which colors are adjusted. Leave it centered unless you specifically want to change that trade."
  },
  "settings-drawer.solver.region-method": {
    "title": "Region method",
    "body": "**Region method** controls the shape of the starting color areas. **Image regions** follows irregular image boundaries. **Superpixels** makes more compact, evenly shaped patches. **Fixed grid** uses regular square cells regardless of the image, so its boundaries can be more visible.\n\nThese are only the starting regions. **Local recipe corrections** can add smaller patches, and **Boundary mutation** can move their edges afterward. Strong refinement can therefore make the final results from different Region methods look more alike."
  },
  "settings-drawer.solver.region-target": {
    "title": "Color region target",
    "body": "**Color region target** sets the intended size of the starting color areas. Smaller values can follow finer structure; larger values tend to make broader, simpler patches.\n\nPrisma may round this target up when your printer needs wider color features."
  },
  "settings-drawer.solver.planning-scale": {
    "title": "Region planning scale",
    "body": "**Region planning scale** controls how much image detail is available while those areas are planned. **1×** uses the full solve grid. Higher values plan from a reduced image, which can lose fine structure.\n\nBoth controls shape the initial patchwork, not necessarily the final one. **Boundary mutation** can substantially reshape its edges, while **Local recipe corrections** can add smaller areas when planning scale is above 1×."
  },
  "settings-drawer.solver.neutral-field": {
    "title": "Neutral-field protection",
    "body": "Pale or gray areas can split into neighboring regions with visibly different tints even when the source looked even. **Neutral-field protection** narrows the solver's choices in those areas to keep them more consistent.\n\nEnable it, then choose **Narrow**, **Standard**, or **Broad** to include progressively more colorful areas. With Advanced settings displayed, editing the raw cutoff to another value is shown as **Custom**."
  },
  "settings-drawer.solver.local-corrections": {
    "title": "Local recipe corrections",
    "body": "**Local recipe corrections** can restore small details lost by a coarser **Region planning scale**, but may create extra patches inside otherwise uniform regions.\n\nIt has no effect at **1×**. It can act only when Region planning scale is higher, and **Boundary mutation** may move the resulting edges afterward."
  },
  "settings-drawer.solver.boundary-mutation": {
    "title": "Boundary mutation",
    "body": "**Boundary mutation** lets edge points borrow a neighboring region's recipe when it matches the image better.\n\nThis can significantly reshape the planned boundaries and make the different Region methods look less distinct in the final preview."
  },
  "settings-drawer.solver.mutation-controls": {
    "title": "Mutation passes and min gain",
    "body": "**Mutation passes** controls how far that refinement can travel. Each pass can move an edge by one solve point, and it stops early when no more improvements are found.\n\n**Mutation min gain** filters out weak connected adjustments. The default is 0.010 dE. Higher values accept fewer, stronger changes; lower values admit subtler ones."
  },
  "settings-drawer.white-cap.intro": {
    "title": "White Cap",
    "body": "Every color stack is covered by white. That cover is the surface you look through, and its shape is the relief you see on the finished print.\n\nThese settings describe Color mode. Luminance mode gives the cap a different job, and has its own section in this tour."
  },
  "settings-drawer.white-cap.cap-style": {
    "title": "Boundary cap style",
    "body": "**Boundary cap style** decides how Prisma balances the preview against the outer surface. **Detail Aware** protects the solved appearance while smoothing the cap. **Smooth** gives more priority to a continuous surface.\n\nIt works together with **Smoothing radius** and **Max Detail Layers**, and it changes how the final relief is divided between the boundary cap and the detail cap."
  },
  "settings-drawer.white-cap.appearance-budget": {
    "title": "Appearance budget",
    "body": "With **Detail Aware**, **Appearance budget** controls that trade. Lower values keep the result closer to the solved appearance; higher values allow more smoothing and more visible change.\n\nLeave it at the 0.004 dE default unless you deliberately want a different balance."
  },
  "settings-drawer.white-cap.smoothing-radius": {
    "title": "Smoothing radius",
    "body": "**Smoothing radius** is the physical distance over which the boundary cap's top surface is smoothed. Its underside still fits the colors exactly.\n\nLower values keep more relief; higher values make the surface smoother. The default is 1.0 mm. If smoothing creates dark outlines around raised areas, reduce it toward 0.5 mm.\n\nIn **Detail Aware**, **Appearance budget** limits how much visible change this smoothing may introduce."
  },
  "settings-drawer.white-cap.detail-depth": {
    "title": "Max Detail Layers",
    "body": "Above the boundary cap, the **detail cap** adds localized white relief. **Max Detail Layers** is the most extra layers it may use — a ceiling, not a target. Zero permits no optional detail relief, and any higher value may still use less.\n\nRaise it when fine relief is missing. A higher value only permits more relief; it cannot create height already used by the base, colors, or boundary cap. **Max Thickness** and **Boundary cap style** can therefore keep a higher value from making any visible difference."
  },
  "settings-drawer.luminance.intro": {
    "title": "Luminance Mode",
    "body": "**Luminance** keeps the colored-recipe solve, then builds the white cap from the image's brightness. Broad shading can be carried by the boundary cap, while finer changes can be added as detail relief.\n\nPrisma has switched Solve Mode to **Luminance** so you can see what changes. It goes back to **Color** when you leave this section."
  },
  "settings-drawer.luminance.what-changes": {
    "title": "What Luminance changes",
    "body": "Because the cap is now an active part of reconstructing the image, settings that change brightness, white-material behavior, available height, or cap shape can have a more direct effect.\n\nColor-region controls still shape the colored patches, but do not by themselves limit fine tonal structure in the cap."
  },
  "settings-drawer.luminance.drawer-changes": {
    "title": "What the drawer shows now",
    "body": "Luminance mode always uses the **Smooth** boundary cap. **Boundary cap style** and **Appearance budget** are therefore hidden, and **Shading balance** appears in their place."
  },
  "settings-drawer.luminance.max-total-thickness": {
    "title": "Max Thickness in Luminance",
    "body": "In Luminance mode, **Max Thickness** limits both the color stacks and the white-cap height available to carry brightness and detail.\n\nThe base, colors, boundary cap, and detail cap must all fit within this height. A larger budget can give the cap more tonal range, but only where the color stack leaves room above it."
  },
  "settings-drawer.luminance.white-filament": {
    "title": "Base/Cap Filament in Luminance",
    "body": "Luminance mode uses the selected white filament to predict how cap thickness changes the transmitted light. Changing it can therefore reshape the cap as well as change the color recipes and preview.\n\nChoose the white filament you will actually use."
  },
  "settings-drawer.luminance.base-thickness": {
    "title": "Base Thickness in Luminance",
    "body": "**Base Thickness** still sets the solid plate beneath the colors. In Luminance mode it also affects how Prisma translates image brightness into white-cap height.\n\nA thicker base leaves less total height for both the colors and the brightness-carrying cap."
  },
  "settings-drawer.luminance.preprocessing": {
    "title": "Preprocessing that changes brightness",
    "body": "Any preprocessing that changes brightness also changes the relief Luminance mode asks the white cap to carry. This is especially important for **Noise Reduction**, **Print-Scale Smoothing**, **Flat-Area Smoothing**, and **Palette Tone Fit**.\n\nTheir controls still work as previously described. The difference is that smoothed or remapped brightness can now become a smoother or simpler cap surface, not only a different color solve."
  },
  "settings-drawer.luminance.appearance-model": {
    "title": "Appearance Model in Luminance",
    "body": "In Luminance mode, the **Appearance Model** predicts both the layered colors and how different white-cap thicknesses transmit light. Changing models can therefore change the cap relief as well as the color solve.\n\n**Color Model v2** remains the normal choice."
  },
  "settings-drawer.luminance.white-point-rescale": {
    "title": "White-point rescale in Luminance",
    "body": "**White-point rescale** still shifts the image toward the brightest printable white. In Luminance mode, that adjusted brightness also guides the white-cap relief, so the setting can reshape broad areas of the surface.\n\nLeave it off unless pale or gray areas need the correction it makes. **Palette Tone Fit** acts first, so using both can compound the change before the cap is built."
  },
  "settings-drawer.luminance.chroma-weight": {
    "title": "Chroma weight in Luminance",
    "body": "**Chroma weight** still changes how the colored recipes trade brightness against hue and vividness. It does not change the white cap's separate brightness target.\n\nThis means the cap can carry some of the light-and-dark pattern even when the colored stack is allowed to favor color. The final result still depends on how much cap height is available."
  },
  "settings-drawer.luminance.region-controls": {
    "title": "Color-region controls in Luminance",
    "body": "**Region method**, **Color region target**, **Region planning scale**, and the region-refinement controls still shape the colored recipe patches.\n\nThe white cap's brightness and detail are built afterward and are not limited to the starting color-region boundaries. Broader color regions can therefore retain some fine tonal structure in the cap, although their stack height still affects how much cap can fit above them."
  },
  "settings-drawer.luminance.min-cap-layers": {
    "title": "Min Cap Layers in Luminance",
    "body": "In Luminance mode, **Min Cap Layers** is both the required white cover and the starting thickness of the cap that carries brightness.\n\nRaising it leaves less room for the cap to vary above that minimum, as well as less height for color and detail. This can reduce the tonal range Luminance mode can express through the cap."
  },
  "settings-drawer.luminance.shading-balance": {
    "title": "Shading balance",
    "body": "**Shading balance** controls how Prisma divides brightness between the smooth boundary cap and the finer detail cap. Toward **Smooth**, the boundary cap may carry more of the tonal range. Toward **Detail**, it stays closer to its minimum while more of the remaining variation is left for detail relief.\n\nThe percentage is a balance, not a literal share of the final cap height or surface. Its result also depends on Smoothing radius, Max Detail Layers, the white filament, and the available thickness."
  },
  "settings-drawer.luminance.shading-balance-suggest": {
    "title": "Suggest",
    "body": "**Suggest** uses the current Image to choose a starting value for Shading balance.\n\nIt does not change the other settings that may limit how fully that balance can be used."
  },
  "settings-drawer.luminance.smoothing-radius": {
    "title": "Smoothing radius in Luminance",
    "body": "**Smoothing radius** still controls how broadly the boundary cap's top surface is smoothed. In Luminance mode, it also helps separate broad shading from fine detail.\n\nLarger values keep smaller brightness changes out of the smooth boundary cap, leaving more of them for the detail cap to carry. The visible result therefore depends strongly on **Shading balance** and **Max Detail Layers**."
  },
  "settings-drawer.luminance.detail-depth": {
    "title": "Max Detail Layers in Luminance",
    "body": "In Luminance mode, the detail cap carries fine brightness variation left above the smooth boundary cap. **Max Detail Layers** limits how many extra layers it may use.\n\nA higher value can preserve more fine tonal relief, but it remains a ceiling. It cannot create height already used by the base, colors, or boundary cap, and printability may reject relief that is too small to print reliably."
  }
});
