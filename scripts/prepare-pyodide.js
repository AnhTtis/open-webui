import { createHash } from 'crypto';
import { access, cp, mkdir, readFile, readdir, rename, rm, writeFile } from 'fs/promises';
import { dirname, join, resolve } from 'path';
import { fileURLToPath } from 'url';

import { loadPyodide } from 'pyodide';
import { Agent, ProxyAgent, setGlobalDispatcher } from 'undici';

const packages = [
	'micropip',
	'packaging',
	'requests',
	'beautifulsoup4',
	'numpy',
	'pandas',
	'matplotlib',
	'scikit-learn',
	'scipy',
	'regex',
	'sympy',
	'tiktoken',
	'seaborn',
	'pytz',
	'black',
	'openai',
	'openpyxl'
];

// Pure-Python packages whose wheels must be saved beside the Pyodide runtime
// so that the browser can install them without contacting PyPI.
const pypiPackages = ['black', 'pathspec', 'mypy_extensions', 'pytokens'];

const scriptPath = fileURLToPath(import.meta.url);
const rootDir = resolve(dirname(scriptPath), '..');
const staticDir = join(rootDir, 'static');
const outputDir = join(staticDir, 'pyodide');
const pyodideSourceDir = join(rootDir, 'node_modules', 'pyodide');
const sentinelName = '.prepared.json';
const sentinelSchemaVersion = 1;
const networkTimeoutMs = parsePositiveInteger(process.env.PYODIDE_PREPARE_TIMEOUT_MS, 60_000);

function parsePositiveInteger(value, fallback) {
	const parsed = Number.parseInt(value ?? '', 10);
	return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function sha256(value) {
	return createHash('sha256').update(value).digest('hex');
}

async function pathExists(path) {
	try {
		await access(path);
		return true;
	} catch {
		return false;
	}
}

async function writeFileAtomic(path, data) {
	const temporaryPath = `${path}.tmp-${process.pid}-${Date.now()}`;
	await writeFile(temporaryPath, data);
	await rename(temporaryPath, path);
}

async function withTimeout(promise, label) {
	let timeout;
	const timeoutPromise = new Promise((_, reject) => {
		timeout = setTimeout(
			() => reject(new Error(`${label} timed out after ${networkTimeoutMs}ms`)),
			networkTimeoutMs
		);
		timeout.unref?.();
	});

	try {
		return await Promise.race([promise, timeoutPromise]);
	} finally {
		clearTimeout(timeout);
	}
}

async function fetchJson(url) {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), networkTimeoutMs);
	timeout.unref?.();

	try {
		const response = await fetch(url, { signal: controller.signal });
		if (!response.ok) {
			throw new Error(`Request failed with HTTP ${response.status}: ${url}`);
		}
		return await response.json();
	} catch (error) {
		if (error?.name === 'AbortError') {
			throw new Error(`Request timed out after ${networkTimeoutMs}ms: ${url}`, { cause: error });
		}
		throw error;
	} finally {
		clearTimeout(timeout);
	}
}

async function fetchBuffer(url) {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), networkTimeoutMs);
	timeout.unref?.();

	try {
		const response = await fetch(url, { signal: controller.signal });
		if (!response.ok) {
			throw new Error(`Request failed with HTTP ${response.status}: ${url}`);
		}
		return Buffer.from(await response.arrayBuffer());
	} catch (error) {
		if (error?.name === 'AbortError') {
			throw new Error(`Request timed out after ${networkTimeoutMs}ms: ${url}`, { cause: error });
		}
		throw error;
	} finally {
		clearTimeout(timeout);
	}
}

/** Load network proxy configuration from the standard environment variables. */
function initNetworkDispatcher() {
	const allProxy = process.env.all_proxy || process.env.ALL_PROXY;
	const httpsProxy = process.env.https_proxy || process.env.HTTPS_PROXY;
	const httpProxy = process.env.http_proxy || process.env.HTTP_PROXY;
	const preferredProxy = httpsProxy || allProxy || httpProxy;

	if (preferredProxy?.startsWith('http')) {
		try {
			const proxyUrl = new URL(preferredProxy).toString();
			setGlobalDispatcher(new ProxyAgent({ uri: proxyUrl }));
			console.log(`Using network proxy ${proxyUrl}`);
			return;
		} catch {
			console.warn(`Ignoring invalid network proxy URL: ${preferredProxy}`);
		}
	}

	setGlobalDispatcher(
		new Agent({
			connect: { timeout: networkTimeoutMs },
			headersTimeout: networkTimeoutMs,
			bodyTimeout: networkTimeoutMs
		})
	);
}

async function getPreparationIdentity() {
	const pyodidePackage = JSON.parse(await readFile(join(pyodideSourceDir, 'package.json'), 'utf8'));
	const scriptHash = sha256(await readFile(scriptPath));
	const preparationHash = sha256(
		JSON.stringify({
			sentinelSchemaVersion,
			pyodideVersion: pyodidePackage.version,
			packages,
			pypiPackages,
			scriptHash
		})
	);

	return {
		pyodideVersion: pyodidePackage.version,
		preparationHash
	};
}

async function isCurrentPreparation(identity) {
	try {
		const sentinel = JSON.parse(await readFile(join(outputDir, sentinelName), 'utf8'));
		if (
			sentinel.schemaVersion !== sentinelSchemaVersion ||
			sentinel.pyodideVersion !== identity.pyodideVersion ||
			sentinel.preparationHash !== identity.preparationHash
		) {
			return false;
		}

		const requiredFiles = [
			'package.json',
			'pyodide-lock.json',
			'pyodide.asm.wasm',
			'pyodide.mjs',
			'python_stdlib.zip'
		];
		return (
			await Promise.all(requiredFiles.map((name) => pathExists(join(outputDir, name))))
		).every(Boolean);
	} catch {
		return false;
	}
}

async function copyPyodide(stagingDir) {
	console.log('Copying the Pyodide runtime into the staging directory');
	for (const entry of await readdir(pyodideSourceDir, { withFileTypes: true })) {
		await cp(join(pyodideSourceDir, entry.name), join(stagingDir, entry.name), {
			recursive: entry.isDirectory()
		});
	}
}

async function installPackages(stagingDir) {
	console.log('Loading Pyodide and preparing browser packages');
	const pyodide = await withTimeout(
		loadPyodide({ packageCacheDir: stagingDir }),
		'Loading Pyodide'
	);

	await withTimeout(pyodide.loadPackage('micropip'), 'Loading micropip');
	const micropip = pyodide.pyimport('micropip');

	try {
		for (const packageName of packages) {
			console.log(`Installing Pyodide package: ${packageName}`);
			await withTimeout(micropip.install(packageName), `Installing ${packageName}`);
		}

		const lockFile = await micropip.freeze();
		await writeFileAtomic(join(stagingDir, 'pyodide-lock.json'), lockFile);
	} finally {
		micropip.destroy?.();
	}
}

async function downloadPyPIWheels(stagingDir) {
	const lockPath = join(stagingDir, 'pyodide-lock.json');
	const lockData = JSON.parse(await readFile(lockPath, 'utf8'));

	for (const packageName of pypiPackages) {
		console.log(`Fetching PyPI metadata for ${packageName}`);
		const metadata = await fetchJson(`https://pypi.org/pypi/${packageName}/json`);
		const wheel = (metadata.urls || []).find(
			(file) => file.filename.endsWith('.whl') && file.filename.includes('py3-none-any')
		);
		if (!wheel) {
			throw new Error(`No pure-Python wheel found for ${packageName}==${metadata.info.version}`);
		}

		console.log(`Downloading ${wheel.filename}`);
		const buffer = await fetchBuffer(wheel.url);
		const expectedHash = wheel.digests?.sha256;
		const actualHash = sha256(buffer);
		if (expectedHash && actualHash !== expectedHash) {
			throw new Error(`SHA-256 mismatch for ${wheel.filename}`);
		}
		await writeFileAtomic(join(stagingDir, wheel.filename), buffer);

		const normalizedName = packageName.replace(/-/g, '_');
		const existingEntry = lockData.packages[normalizedName] || {};
		lockData.packages[normalizedName] = {
			...existingEntry,
			name: normalizedName,
			version: metadata.info.version,
			file_name: wheel.filename,
			install_dir: existingEntry.install_dir || 'site',
			sha256: expectedHash || actualHash,
			package_type: existingEntry.package_type || 'package',
			imports: existingEntry.imports || [normalizedName],
			depends: existingEntry.depends || []
		};
	}

	await writeFileAtomic(lockPath, `${JSON.stringify(lockData, null, 2)}\n`);
}

async function replaceOutputDirectory(stagingDir) {
	const backupDir = join(staticDir, `.pyodide.backup-${process.pid}-${Date.now()}`);
	const hadExistingOutput = await pathExists(outputDir);

	if (hadExistingOutput) {
		await rename(outputDir, backupDir);
	}

	try {
		await rename(stagingDir, outputDir);
	} catch (error) {
		if (hadExistingOutput && !(await pathExists(outputDir))) {
			await rename(backupDir, outputDir);
		}
		throw error;
	}

	if (hadExistingOutput) {
		try {
			await rm(backupDir, { recursive: true, force: true });
		} catch (error) {
			console.warn(
				`Prepared output is ready, but the old backup could not be removed: ${backupDir}`
			);
			console.warn(error);
		}
	}
}

async function main() {
	initNetworkDispatcher();
	const identity = await getPreparationIdentity();

	if (await isCurrentPreparation(identity)) {
		console.log(`Pyodide ${identity.pyodideVersion} is already prepared; nothing to do.`);
		return;
	}

	await mkdir(staticDir, { recursive: true });
	const stagingDir = join(staticDir, `.pyodide.prepare-${process.pid}-${Date.now()}`);
	await mkdir(stagingDir);

	try {
		await copyPyodide(stagingDir);
		await installPackages(stagingDir);
		await downloadPyPIWheels(stagingDir);
		await writeFileAtomic(
			join(stagingDir, sentinelName),
			`${JSON.stringify(
				{
					schemaVersion: sentinelSchemaVersion,
					pyodideVersion: identity.pyodideVersion,
					preparationHash: identity.preparationHash,
					preparedAt: new Date().toISOString()
				},
				null,
				2
			)}\n`
		);
		await replaceOutputDirectory(stagingDir);
		console.log(`Prepared Pyodide ${identity.pyodideVersion} in ${outputDir}`);
	} finally {
		await rm(stagingDir, { recursive: true, force: true });
	}
}

await main();
