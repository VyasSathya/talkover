import React from 'react';
import {Composition} from 'remotion';
import {Demo, INTRO_S, OUTRO_S, Timeline} from './Demo';

const empty: Timeline = {
  fps: 30, duration: 10, source: {w: 3440, h: 1440}, segments: [], cameras: [], captions: [], cursor: [],
  panel: null, title: 'Talkover', subtitle: '', url: '', synthetic: false, music: false,
};

export const Root: React.FC = () => (
  <Composition
    id="Demo"
    component={Demo}
    width={1920}
    height={1080}
    fps={30}
    durationInFrames={Math.ceil((INTRO_S + empty.duration + OUTRO_S) * 30)}
    defaultProps={{timeline: empty}}
    calculateMetadata={({props}) => ({
      durationInFrames: Math.ceil((INTRO_S + props.timeline.duration + OUTRO_S) * props.timeline.fps),
      fps: props.timeline.fps,
    })}
  />
);
