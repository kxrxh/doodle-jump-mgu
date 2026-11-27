mod decode;
mod elf;
mod flow;
mod integer;
mod lifter;
mod memory;
mod vfp;

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::decode::Decoder;
use crate::elf::{HELPER_BASE, Image};
use crate::flow::discover;
use crate::lifter::{Lifter, name};

type BuildResult<T> = Result<T, Box<dyn std::error::Error>>;

const INCLUDE: &str = "#include \"lifted.h\"\n";

#[derive(Deserialize)]
struct Profile {
    engine_sha256: String,
    entries: BTreeMap<String, bool>,
}

#[derive(Serialize)]
struct Report {
    engine_sha256: String,
    functions: usize,
    complete_functions: usize,
    discovery_converged: bool,
    instructions: usize,
    supported_instructions: usize,
    continuations: usize,
    unresolved_calls: Vec<String>,
    unsupported: Vec<(String, usize)>,
}

fn trace_address(text: &str) -> Option<u32> {
    let text = text.trim();
    let text = text.strip_prefix('+').unwrap_or(text);
    let (radix, digits) = match text.as_bytes().get(..2) {
        Some(b"0x" | b"0X") => (16, &text[2..]),
        Some(b"0o" | b"0O") => (8, &text[2..]),
        Some(b"0b" | b"0B") => (2, &text[2..]),
        _ => (10, text),
    };
    u32::from_str_radix(digits, radix).ok()
}

fn apply_profile(image: &mut Image<'_>, path: &Path, digest: &str) -> BuildResult<()> {
    let profile: Profile = serde_json::from_slice(&fs::read(path)?)?;
    if profile.engine_sha256 != digest {
        return Err("Trace does not match the original engine".into());
    }
    for (text, thumb) in profile.entries {
        let address = trace_address(&text)
            .filter(|address| address & 1 == 0 && image.executable(*address))
            .ok_or("Invalid trace entry point")?;
        image.entries.insert(address, thumb);
    }
    let mut starts: BTreeMap<_, _> = image
        .functions
        .values()
        .map(|f| (f.address, f.thumb))
        .collect();
    starts.extend(
        image
            .entries
            .iter()
            .map(|(&address, &thumb)| (address, thumb)),
    );
    image.set_ranges(&starts);
    Ok(())
}

fn generate(
    original: &[u8],
    helpers: &[u8],
    output: &Path,
    profile: Option<&Path>,
) -> BuildResult<Report> {
    let digest = format!("{:x}", Sha256::digest(original));
    let mut image = Image::parse(original)?;
    if let Some(profile) = profile {
        apply_profile(&mut image, profile, &digest)?;
    }
    let decoder = Decoder::new()?;
    let discovery_converged = discover(&mut image, &decoder)?;

    let (helper_code, helper_functions) = elf::helpers(helpers)?;
    let helper_starts = helper_functions
        .iter()
        .map(|f| f.address)
        .collect::<BTreeSet<_>>();
    let mut functions = image
        .functions
        .values()
        .copied()
        .chain(helper_functions)
        .collect::<Vec<_>>();
    functions.sort_by_key(|function| function.address);
    let mut lifter = Lifter::new(&decoder, &image.noreturn);
    let mut complete_functions = 0;
    let mut shards = std::array::from_fn::<_, 32, _>(|_| INCLUDE.to_owned());
    for (index, function) in functions.iter().copied().enumerate() {
        let body = if helper_starts.contains(&function.address) {
            let offset = (function.address - HELPER_BASE) as usize;
            std::borrow::Cow::Borrowed(&helper_code[offset..offset + function.size as usize])
        } else {
            // ELF relocations are applied at run time.
            image.read(function.address, function.size)?
        };
        let (source, complete) = lifter.lift(function, &body)?;
        complete_functions += usize::from(complete);
        writeln!(shards[index % 32], "{source}")?;
    }

    let defined: BTreeSet<_> = functions.iter().map(|function| function.address).collect();
    let missing: BTreeSet<_> = lifter.referenced.difference(&defined).copied().collect();
    let mut continuations = lifter.continuations;
    continuations.retain(|address, _| !defined.contains(address));
    let mut header = "#pragma once\n#include \"arm32_context.h\"\n".to_owned();
    for address in defined.union(&missing).copied() {
        writeln!(header, "void {}(Arm32Ctx* c);", name(address))?;
    }
    let resumes: BTreeSet<_> = continuations.values().map(|&(owner, _)| owner).collect();
    for address in resumes {
        writeln!(
            header,
            "void {}_resume(Arm32Ctx* c, uint32_t pc);",
            name(address)
        )?;
    }
    let mut dispatch = INCLUDE.to_owned();
    for &address in &missing {
        writeln!(
            dispatch,
            "void {}(Arm32Ctx* c) {{ arc_dispatch(c, {address:#x}u); }}",
            name(address)
        )?;
    }
    let mut entries: BTreeMap<_, _> = functions
        .iter()
        .map(|f| (f.address, (name(f.address), f.thumb)))
        .collect();
    for (&address, &(owner, thumb)) in &continuations {
        writeln!(
            dispatch,
            "static void resume_{address:08x}(Arm32Ctx* c) {{ {}_resume(c, {address:#x}u); }}",
            name(owner)
        )?;
        entries.insert(address, (format!("resume_{address:08x}"), thumb));
    }
    dispatch.push_str("struct Entry { uint32_t address; void (*call)(Arm32Ctx*); unsigned thumb; };\nstatic const struct Entry entries[] = {\n");
    for (address, (name, thumb)) in entries {
        writeln!(dispatch, "{{{address:#x}u,{name},{}u}},", u8::from(thumb))?;
    }
    dispatch.push_str(
        r#"};
void doodle_resolve(Arm32Ctx* c, uint32_t target) {
    uint32_t address = target & ~1u;
    size_t lo = 0, hi = sizeof(entries)/sizeof(entries[0]);
    while(lo < hi) { size_t mid = lo + (hi-lo)/2;
        if(entries[mid].address < address) lo=mid+1; else hi=mid; }
    if(lo == sizeof(entries)/sizeof(entries[0]) || entries[lo].address != address) {
        arc_dispatch_miss(c,target); return;
    }
    if ((target & 1u) != entries[lo].thumb) { arc_trap(c,"AOT instruction mode mismatch"); return; }
    entries[lo].call(c);
}
"#,
    );
    writeln!(
        dispatch,
        "const char* doodle_engine_sha256(void) {{ return \"{digest}\"; }}"
    )?;
    fs::create_dir_all(output)?;
    fs::write(output.join("lifted.h"), header)?;
    fs::write(output.join("module.c"), dispatch)?;
    for (index, shard) in shards.into_iter().enumerate() {
        fs::write(output.join(format!("lifted_{index:02}.c")), shard)?;
    }
    lifter
        .unsupported
        .sort_by_key(|(_, count)| std::cmp::Reverse(*count));
    Ok(Report {
        engine_sha256: digest,
        functions: functions.len(),
        complete_functions,
        discovery_converged,
        instructions: lifter.instructions,
        supported_instructions: lifter.supported,
        continuations: continuations.len(),
        unresolved_calls: missing
            .iter()
            .map(|address| format!("{address:#x}"))
            .collect(),
        unsupported: lifter.unsupported,
    })
}

fn run() -> BuildResult<()> {
    let args = std::env::args_os()
        .skip(1)
        .map(PathBuf::from)
        .collect::<Vec<_>>();
    let (original, helpers, output, profile) = match args.as_slice() {
        [original, helpers, output] => (original, helpers, output, None),
        [original, helpers, output, profile] => {
            (original, helpers, output, Some(profile.as_path()))
        }
        _ => return Err("Usage: doodle-recompile ORIGINAL ARM_HELPERS OUTPUT [PROFILE]".into()),
    };
    let original = fs::read(original)?;
    let helpers = fs::read(helpers)?;
    let report = generate(&original, &helpers, output, profile)?;
    let json = format!("{}\n", serde_json::to_string_pretty(&report)?);
    fs::write(output.join("report.json"), &json)?;
    print!("{json}");
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("doodle-recompile: {error}");
            ExitCode::FAILURE
        }
    }
}
