import { NextApiRequest, NextApiResponse } from 'next';

let state = {
  components: {},
  adjGraph: {},
};

export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method === 'GET') {
    res.status(200).json(state);
  } else if (req.method === 'POST') {
    const { components, adjGraph } = req.body;
    if (components) state.components = components;
    if (adjGraph) state.adjGraph = adjGraph;
    res.status(200).json(state);
  } else {
    res.status(405).end();
  }
}
