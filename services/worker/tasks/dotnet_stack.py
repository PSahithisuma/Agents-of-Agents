"""dotnet_webapi stack profile: deterministic ASP.NET Core 8 + EF Core (SQLite) CRUD generation from the Spec IR, gated by dotnet build + dotnet test."""
import os, re, json, shutil, asyncio, logging

logger = logging.getLogger(__name__)
DESC = "ASP.NET Core 8 + EF Core + xUnit"
GATE_DESC = "dotnet build + dotnet test"
DOTNET = "/usr/share/dotnet/dotnet"
_RESERVED = {"Id", "TenantId", "CreatedAt", "UpdatedAt"}
_CLASH = {"Task", "File", "Path", "Directory", "Controller", "Program", "Action", "Type", "Timer", "Thread", "Object", "String", "List", "Guid", "Random", "Environment", "Console", "Convert", "Math", "Enum", "Attribute", "Exception"}
_TYPES = {
    "string": ("string", '"sample"'),
    "int": ("int?", "1"),
    "uuid": ("string", '"00000000-0000-4000-8000-000000000001"'),
    "decimal": ("double?", "10.5"),
    "bool": ("bool?", "true"),
    "datetime": ("DateTime?", '"2026-01-01T00:00:00"'),
    "enum": ("string", '"ACTIVE"'),
}


def _words(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(s or ""))
    return [w for w in re.split(r"[^A-Za-z0-9]+", s) if w]


def _pascal(s):
    p = "".join(w[:1].upper() + w[1:] for w in _words(s))
    if not p or not p[0].isalpha():
        p = "E" + p
    return p


def _snake(s):
    x = "_".join(w.lower() for w in _words(s))
    return x if x and x[0].isalpha() else "f_" + x


def _col(t):
    return _TYPES.get(str(t or "string"), _TYPES["string"])


def _entities(spec):
    out, names, paths = [], set(), set()
    for e in (spec.get("domain_model") or {}).get("entities") or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        name = _pascal(e["name"])
        if name in _CLASH:
            name += "Entity"
        path = _snake(e.get("table") or e["name"])
        if name in names or path in paths:
            continue
        fields, fn = [], set()
        for f in e.get("fields") or []:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            n = _pascal(f["name"])
            if n in _RESERVED or n == name:
                n += "Value"
            if n in fn:
                continue
            fn.add(n)
            fields.append((n, f.get("type")))
        names.add(name)
        paths.add(path)
        out.append({"name": name, "path": path, "fields": fields})
    return out


def _camel(n):
    return n[:1].lower() + n[1:]


def generate_files(spec):
    ents = _entities(spec)
    app = spec.get("app_name") or "generated-app"
    f = {}
    f[".gitignore"] = "bin/\nobj/\n*.db\n"
    f["README.md"] = (f"# {app}\n\nASP.NET Core 8 service generated from the Spec IR by Agents of Agents (dotnet_webapi profile).\n\n"
                      f"Entities: {', '.join(e['name'] for e in ents)}\n\n```\ndotnet test tests/Api.Tests/Api.Tests.csproj\n"
                      "dotnet run --project src/Api\n```\n\nSet DATABASE_URL (default: local SQLite file). This is a generated template, not a compliance certification.\n")
    f["Dockerfile"] = ("FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build\nWORKDIR /src\nCOPY . .\n"
                       "RUN dotnet publish src/Api/Api.csproj -c Release -o /out\n"
                       "FROM mcr.microsoft.com/dotnet/aspnet:8.0\nWORKDIR /app\nCOPY --from=build /out .\n"
                       "ENV ASPNETCORE_URLS=http://+:8080\nEXPOSE 8080\nENTRYPOINT [\"dotnet\", \"Api.dll\"]\n")
    f["src/Api/Api.csproj"] = ('<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n'
                               '    <Nullable>disable</Nullable>\n    <ImplicitUsings>enable</ImplicitUsings>\n    <RootNamespace>Api</RootNamespace>\n  </PropertyGroup>\n'
                               '  <ItemGroup>\n    <PackageReference Include="Microsoft.EntityFrameworkCore.Sqlite" Version="8.0.11" />\n'
                               '    <PackageReference Include="Swashbuckle.AspNetCore" Version="6.9.0" />\n  </ItemGroup>\n</Project>\n')
    f["tests/Api.Tests/Api.Tests.csproj"] = (
        '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n    <Nullable>disable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n    <IsPackable>false</IsPackable>\n  </PropertyGroup>\n  <ItemGroup>\n'
        '    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" />\n    <PackageReference Include="xunit" Version="2.9.2" />\n'
        '    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />\n'
        '    <PackageReference Include="Microsoft.AspNetCore.Mvc.Testing" Version="8.0.11" />\n  </ItemGroup>\n'
        '  <ItemGroup>\n    <ProjectReference Include="../../src/Api/Api.csproj" />\n  </ItemGroup>\n</Project>\n')
    f["src/Api/Program.cs"] = (
        "using Api.Data;\nusing Microsoft.EntityFrameworkCore;\n\n"
        "var builder = WebApplication.CreateBuilder(args);\n"
        "var cs = builder.Configuration[\"DATABASE_URL\"] ?? \"Data Source=app.db\";\n"
        "builder.Services.AddDbContext<AppDbContext>(o => o.UseSqlite(cs));\n"
        "builder.Services.AddControllers();\n"
        "builder.Services.AddEndpointsApiExplorer();\n"
        "builder.Services.AddSwaggerGen();\n\n"
        "var app = builder.Build();\n"
        "var pathBase = builder.Configuration[\"PATH_BASE\"];\n"
        "if (!string.IsNullOrEmpty(pathBase)) app.UsePathBase(pathBase);\n"
        "using (var scope = app.Services.CreateScope())\n{\n    scope.ServiceProvider.GetRequiredService<AppDbContext>().Database.EnsureCreated();\n}\n"
        "app.UseSwagger();\napp.UseSwaggerUI();\n"
        "app.MapGet(\"/health\", () => Results.Ok(new { status = \"ok\" }));\napp.MapControllers();\napp.Run();\n\npublic partial class Program { }\n")
    ctx = ["using Api.Models;", "using Microsoft.EntityFrameworkCore;", "", "namespace Api.Data;", "",
           "public class AppDbContext : DbContext", "{", "    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options) { }"]
    for e in ents:
        N = e["name"]
        ctx.append(f"    public DbSet<{N}> {N}Set => Set<{N}>();")
        props = ["    public string Id { get; set; } = Guid.NewGuid().ToString();", "    public string TenantId { get; set; }"]
        for n, t in e["fields"]:
            props.append(f"    public {_col(t)[0]} {n} {{ get; set; }}")
        props += ["    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;", "    public DateTime UpdatedAt { get; set; } = DateTime.UtcNow;"]
        f[f"src/Api/Models/{N}.cs"] = f"namespace Api.Models;\n\npublic class {N}\n{{\n" + "\n".join(props) + "\n}\n"
        copy = "".join(f"        obj.{n} = body.{n};\n" for n, _ in e["fields"])
        f[f"src/Api/Controllers/{N}Controller.cs"] = (
            "using Api.Data;\nusing Api.Models;\nusing Microsoft.AspNetCore.Mvc;\nusing Microsoft.EntityFrameworkCore;\n\nnamespace Api.Controllers;\n\n"
            f"[ApiController]\n[Route(\"api/v1/{e['path']}\")]\npublic class {N}Controller : ControllerBase\n{{\n"
            "    private readonly AppDbContext _db;\n\n"
            f"    public {N}Controller(AppDbContext db) {{ _db = db; }}\n\n"
            f"    [HttpGet]\n    public async Task<List<{N}>> List([FromQuery] int skip = 0, [FromQuery] int limit = 100)\n"
            f"        => await _db.Set<{N}>().OrderBy(x => x.Id).Skip(skip).Take(Math.Min(limit, 500)).ToListAsync();\n\n"
            f"    [HttpPost]\n    public async Task<IActionResult> Create({N} body)\n    {{\n"
            "        body.Id = Guid.NewGuid().ToString();\n        body.CreatedAt = DateTime.UtcNow;\n        body.UpdatedAt = body.CreatedAt;\n"
            f"        _db.Add(body);\n        await _db.SaveChangesAsync();\n        return Created($\"/api/v1/{e['path']}/{{body.Id}}\", body);\n    }}\n\n"
            f"    [HttpGet(\"{{id}}\")]\n    public async Task<IActionResult> Get(string id)\n    {{\n"
            f"        var obj = await _db.Set<{N}>().FindAsync(id);\n        return obj == null ? NotFound() : Ok(obj);\n    }}\n\n"
            f"    [HttpPut(\"{{id}}\")]\n    public async Task<IActionResult> Update(string id, {N} body)\n    {{\n"
            f"        var obj = await _db.Set<{N}>().FindAsync(id);\n        if (obj == null) return NotFound();\n" + copy +
            "        obj.UpdatedAt = DateTime.UtcNow;\n        await _db.SaveChangesAsync();\n        return Ok(obj);\n    }\n\n"
            f"    [HttpDelete(\"{{id}}\")]\n    public async Task<IActionResult> Delete(string id)\n    {{\n"
            f"        var obj = await _db.Set<{N}>().FindAsync(id);\n        if (obj == null) return NotFound();\n"
            "        _db.Remove(obj);\n        await _db.SaveChangesAsync();\n        return NoContent();\n    }\n}\n")
    ctx.append("}")
    f["src/Api/Data/AppDbContext.cs"] = "\n".join(ctx) + "\n"
    t = ["using System.Net;", "using System.Text;", "using Microsoft.AspNetCore.Mvc.Testing;", "using Xunit;", "",
         "public class ApiTests : IClassFixture<WebApplicationFactory<Program>>", "{", "    private readonly HttpClient _c;", "",
         "    static ApiTests()", "    {",
         "        Environment.SetEnvironmentVariable(\"DATABASE_URL\", \"Data Source=\" + Path.Combine(Path.GetTempPath(), \"t\" + Guid.NewGuid().ToString(\"N\") + \".db\"));",
         "    }", "", "    public ApiTests(WebApplicationFactory<Program> factory) { _c = factory.CreateClient(); }", "",
         "    private static StringContent Body(string json) => new StringContent(json, Encoding.UTF8, \"application/json\");", "",
         "    [Fact]", "    public async Task Health()", "    {", "        var r = await _c.GetAsync(\"/health\");", "        Assert.Equal(HttpStatusCode.OK, r.StatusCode);", "    }"]
    for e in ents:
        payload = "{" + ",".join(f'"{_camel(n)}":{_col(tp)[1]}' for n, tp in e["fields"]) + "}"
        P = e["path"]
        t += ["", "    [Fact]", f"    public async Task Crud_{e['name']}()", "    {", f"        var p = \"/api/v1/{P}\";",
              f"        var payload = \"\"\"{payload}\"\"\";",
              "        var created = await _c.PostAsync(p, Body(payload));", "        Assert.Equal(HttpStatusCode.Created, created.StatusCode);",
              "        var id = System.Text.Json.JsonDocument.Parse(await created.Content.ReadAsStringAsync()).RootElement.GetProperty(\"id\").GetString();",
              "        Assert.Equal(HttpStatusCode.OK, (await _c.GetAsync(p)).StatusCode);",
              "        Assert.Equal(HttpStatusCode.OK, (await _c.GetAsync(p + \"/\" + id)).StatusCode);",
              "        Assert.Equal(HttpStatusCode.OK, (await _c.PutAsync(p + \"/\" + id, Body(payload))).StatusCode);",
              "        Assert.Equal(HttpStatusCode.NoContent, (await _c.DeleteAsync(p + \"/\" + id)).StatusCode);",
              "        Assert.Equal(HttpStatusCode.NotFound, (await _c.GetAsync(p + \"/\" + id)).StatusCode);", "    }"]
    t.append("}")
    f["tests/Api.Tests/ApiTests.cs"] = "\n".join(t) + "\n"
    return f, ents


async def _sh(cmd, cwd, timeout):
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1", DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1",
               DOTNET_CLI_HOME="/tmp/dotnet-home", NUGET_PACKAGES=os.environ.get("NUGET_PACKAGES", "/opt/nuget-cache"))
    p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timeout"
    return p.returncode, out.decode("utf-8", "replace")


async def run_gate(files, job_id, dotnet=DOTNET):
    d = f"/tmp/dn-{job_id}"
    if not os.path.exists(dotnet):
        return {"compile": {"status": "unavailable"}, "tests": {"status": "unavailable"}}
    shutil.rmtree(d, ignore_errors=True)
    try:
        for path, content in files.items():
            fp = os.path.join(d, path)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w") as fh:
                fh.write(content)
        rc, out = await _sh([dotnet, "build", "src/Api/Api.csproj", "-c", "Release", "--nologo"], d, 900)
        errs = [ln for ln in out.splitlines() if " error " in ln]
        comp = {"status": "ok" if rc == 0 else "fail", "output": ("\n".join(errs[:8]) or out)[-800:]}
        if rc != 0:
            return {"compile": comp, "tests": {"status": "skipped"}}
        rc2, out2 = await _sh([dotnet, "test", "tests/Api.Tests/Api.Tests.csproj", "--nologo"], d, 900)
        ok = re.search(r"Passed:\s+(\d+)", out2)
        bad = re.search(r"Failed:\s+(\d+)", out2)
        tests = {"status": "ok" if rc2 == 0 else "fail", "tests": int(ok.group(1)) if ok else 0,
                 "failures": int(bad.group(1)) if bad else 0, "errors": 0, "output": out2[-1500:]}
        return {"compile": comp, "tests": tests}
    finally:
        shutil.rmtree(d, ignore_errors=True)
