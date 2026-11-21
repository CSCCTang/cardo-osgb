// cardo-osgb 的 Host 半：把包内 skill 注册到 ctx.skills。
// 依据《dsh 插件开发标准（现行版）》5.1、6.1。
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'cardo-osgb'
export const inject = ['skills']

const skillDir = join(dirname(fileURLToPath(import.meta.url)), 'skills', 'cardo-osgb')

function skillBody(raw) {
  // 去掉 YAML frontmatter：模型侧只接正文
  return raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '')
}

export function apply(ctx) {
  // 浏览引擎随包分发：缺了说明安装不完整，提示重装，不中断插件加载。
  if (!existsSync(join(skillDir, 'assets', 'viewer', 'tool', 'caddy.exe'))) {
    console.warn(
      '[cardo-osgb] 包内缺模型浏览引擎 assets/viewer（安装包不完整）：07 将跳过「模型浏览」。\n' +
      '  期望 skills/cardo-osgb/assets/viewer/tool/caddy.exe；请重新安装本插件。'
    )
  }
  ctx.skills.register({
    name: 'cardo-osgb',
    description: "Cardo 低空解析流水线：OSGB 转 3D Tiles、模型体检与现状指标。",
    whenToUse: "用户要把 OSGB 倾斜模型包转成 3D Tiles、做模型体检与现状指标，产出《模型体检报告》《现状指标》《指标说明》时使用。",
    source: 'custom',
    content: skillBody(readFileSync(join(skillDir, 'SKILL.md'), 'utf8')),
    resourceBase: { kind: 'directory', path: skillDir },
  })
}
