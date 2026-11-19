import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip } from 'recharts'

import { radarPoints } from '../skills'
import type { Skills } from '../types'

/** The six skills on one 0-100 radar. Loaded lazily: Recharts is large. */
export default function SkillsRadar({ skills }: { skills: Skills }) {
  return (
    <ResponsiveContainer width="100%" height={300}>
      {/* Room at the sides for the longest label ("Consistency") on a phone. */}
      <RadarChart data={radarPoints(skills)} outerRadius="70%" margin={{ left: 40, right: 40 }}>
        <PolarGrid strokeOpacity={0.3} />
        <PolarAngleAxis dataKey="skill" />
        <PolarRadiusAxis domain={[0, 100]} tickCount={6} angle={90} tick={false} axisLine={false} />
        <Tooltip formatter={(value) => [Number(value).toFixed(1), 'Score']} />
        <Radar
          dataKey="score"
          stroke="#ff66aa"
          fill="#ff66aa"
          fillOpacity={0.35}
          strokeWidth={2}
          isAnimationActive={false}
        />
      </RadarChart>
    </ResponsiveContainer>
  )
}
