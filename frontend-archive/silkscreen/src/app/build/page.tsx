"use client";
import React, { useState, useRef, useEffect } from "react";
import * as d3 from "d3";

export default function Home() {
  const [fullscreen, setFullscreen] = useState(false);
  const [components, setComponents] = useState<any>({});
  const [adjGraph, setAdjGraph] = useState<any>({});
  const [buildStatus, setBuildStatus] = useState<string>("");
  const [layouts, setPcbLayout] = useState<any>({});
  const [finalPcbLayout, setFinalPcbLayout] = useState<any>({});
  const [schematic, setSchematic] = useState<any>("");
  const [solverStatus, setSolverStatus] = useState<string>("");
  // Poll build status from backend
  useEffect(() => {
    let isMounted = true;
    const poll = async () => {
      try {
        const res = await fetch('http://localhost:8000/buildstatus');
        if (!res.ok) throw new Error('Failed to fetch build status');
        const data = await res.json();
        console.log(data);
        if (isMounted) {
          if (JSON.stringify(data.components) != JSON.stringify(components)) {
            setComponents(data.components || {});
          }
          if (JSON.stringify(data.adjGraph) != JSON.stringify(adjGraph)) {
            console.log(adjGraph);
            console.log(JSON.stringify(data.adjGraph), JSON.stringify(adjGraph));
            setAdjGraph(data.adjGraph);
          }
          setBuildStatus(data.status || "");
          setPcbLayout(data.layouts || {});
          setFinalPcbLayout(data.fullLayout || null);
          setSchematic(data.schematic || null);
          setSolverStatus(data.solverStatus || "");
        }
      } catch (err) {
        // Optionally handle error
      }
      if (isMounted) setTimeout(poll, 2000);
    };
    poll();
    return () => { isMounted = false; };
  }, [adjGraph, components]);
  const graphRef = useRef(null);
  const graphContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!graphRef.current) return;
    if (!adjGraph || Object.keys(adjGraph).length === 0) {
      d3.select(graphRef.current).selectAll("*").remove();
      return;
    }
    // Hierarchical node support: if a node has a property 'subgraph', treat it as a graph-node
    type NodeType = { id: string; x?: number; y?: number; fx?: number | null; fy?: number | null; subgraph?: any };
    type LinkType = { source: string; target: string };
    // Example: adjGraph = { A: [B], B: [C], G1: [A], G2: [], ... } and components[G1].subgraph = {...}
    const nodes: NodeType[] = Object.keys(adjGraph).map(id => {
      if (components[id] && components[id].subgraph) {
        return { id, subgraph: components[id].subgraph };
      }
      return { id };
    });
    const links: LinkType[] = Object.entries(adjGraph).flatMap(([source, targets]) =>
      (Array.isArray(targets) ? targets : []).map((target: string) => ({ source, target }))
    );

    const width = fullscreen ? window.innerWidth : 400;
    const height = fullscreen ? window.innerHeight : 300;
    const svgEl = d3.select(graphRef.current)
      .attr("width", width)
      .attr("height", height)
      .style("background", "#161e29");

    // Remove previous graph content
    svgEl.selectAll("g").remove();
    svgEl.selectAll("text").remove();

    const simulation = d3.forceSimulation<NodeType>(nodes)
      .force("link", d3.forceLink<NodeType, LinkType>(links).id((d: NodeType) => d.id).distance(180))
      .force("charge", d3.forceManyBody().strength(-400))
      .force("center", d3.forceCenter(width / 2, height / 2));

    // If fullscreen, move all nodes to center
    if (fullscreen) {
      simulation.force("center", null);
      simulation.force("center", d3.forceCenter(width / 2, height / 2));
      simulation.alpha(1).restart();
    }

    const link = svgEl.append("g")
      .attr("stroke", "#888")
      .attr("stroke-width", 2)
      .selectAll("line")
      .data(links)
      .enter().append("line");

    // Draw nodes
    const nodeGroup = svgEl.append("g");
    const node = nodeGroup.selectAll("g.node")
      .data(nodes)
      .enter().append("g")
      .attr("class", "node");

    // Draw main node circle
    node.append("circle")
      .attr("r", d => d.subgraph ? 60 : 40)
      .attr("fill", d => d.subgraph ? "#242f40" : "#208a1a")
      .attr("stroke", "#fff")
      .attr("stroke-width", 1);

    // If node is a graph-node, draw subnodes inside
    node.filter(d => d.subgraph).each(function(d) {
      const subgraph = d.subgraph;
      const subnodes = Object.keys(subgraph).map(id => ({ id }));
      // Arrange subnodes in a circle inside the parent
      const r = 35;
      subnodes.forEach((sub, i) => {
        const angle = (2 * Math.PI * i) / subnodes.length;
        d3.select(this).append("circle")
          .attr("cx", r * Math.cos(angle))
          .attr("cy", r * Math.sin(angle))
          .attr("r", 20)
          .attr("fill", "#208a1a")
          .attr("stroke", "#fff")
          .attr("stroke-width", 1);
        d3.select(this).append("text")
          .attr("x", r * Math.cos(angle))
          .attr("y", r * Math.sin(angle) + 4)
          .attr("text-anchor", "middle")
          .attr("font-size", "0.5rem")
          .attr("fill", "#fff")
          .text(sub.id);
      });
    });

    // Node labels
    node.append("text")
      .text((d: NodeType) => d.id)
      .attr("text-anchor", "middle")
      .attr("dy", d => d.subgraph ? -70 : 2)
      .attr("font-size", "0.7rem")
      .attr("fill", d => d.subgraph ? "#fff" : "#fff");

    // Drag behavior
    (node as any).call(d3.drag()
      .on("start", function(event, d) {
        const node = d as NodeType;
        if (!event.active) simulation.alphaTarget(0.3).restart();
        node.fx = node.x;
        node.fy = node.y;
      })
      .on("drag", function(event, d) {
        const node = d as NodeType;
        node.fx = event.x;
        node.fy = event.y;
      })
      .on("end", function(event, d) {
        const node = d as NodeType;
        if (!event.active) simulation.alphaTarget(0);
        node.fx = null;
        node.fy = null;
      })
    );

    simulation.on("tick", () => {
      link
        .attr("x1", (d: any) => ((d.source as NodeType).x ?? 0))
        .attr("y1", (d: any) => ((d.source as NodeType).y ?? 0))
        .attr("x2", (d: any) => ((d.target as NodeType).x ?? 0))
        .attr("y2", (d: any) => ((d.target as NodeType).y ?? 0));
      node
        .attr("transform", (d: NodeType) => `translate(${d.x ?? 0},${d.y ?? 0})`);
    });
  }, [adjGraph, components, fullscreen]);
  return (
    <main className="flex min-h-screen flex-col items-center font-mono bg-gray-200">
      <div className="flex min-h-screen flex-col items-center p-10 w-full md:w-1/2">
        <h1 className="text-6xl font-bold my-5 font-serif text-emerald-800">Silkscreen</h1>
        <img src="/logo.png" alt="Silkscreen Logo" className="h-32 mb-5"/>
        <div className="flex items-center border border-emerald-800 rounded-full px-4 py-2 mb-5 bg-green-100">
          <span className="w-2 h-2 rounded-full mr-5 bg-emerald-800 animate-ping"></span>
          <p className="uppercase tracking-widest font-mono text-emerald-800">status: {buildStatus}</p>
        </div>
        <h2 className="text-2xl font-bold my-5 uppercase tracking-widest text-emerald-800 w-full">
          <img src="/component_selection.png" alt="Component Selection" className="h-15 mr-5 inline"/>
          Component Selection →</h2>
        <hr className="border-[0.5px] border-emerald-800 mb-4 w-full"/>
        <div className="w-full">
          {Object.entries(components).map(([key, compObj]) => {
            const comp = compObj as {
              img: string;
              name: string;
              description: string;
              submodules?: Record<string, any>;
            };
            return (
              <div key={key} className="mb-8">
                <div className="flex items-center mb-4">
                  <img src={comp.img} alt={comp.name} className="w-24 h-24 mr-4 rounded-lg border"/>
                  <div>
                    <h3 className="text-xl font-bold">{comp.name}</h3>
                    <p className="text-gray-600">{comp.description}</p>
                  </div>
                </div>
                {comp.submodules && (
                  <div className="ml-8 border-l-2 border-gray-300 pl-4">
                    {Object.entries(comp.submodules).map(([subKey, subCompObj]) => {
                      const subComp = subCompObj as {
                        img: string;
                        name: string;
                        description: string;
                      };
                      return (
                        <div key={subKey} className="flex items-center mb-2">
                          <img src={subComp.img} alt={subComp.name} className="w-12 h-12 mr-4 rounded-lg border"/>
                          <div>
                            <h5 className="font-bold">{subComp.name}</h5>
                            <p className="text-gray-600">{subComp.description}</p>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <hr/>
        <h2 className="text-2xl font-bold my-5 uppercase tracking-widest text-emerald-800 w-full">
          <img src="/netlist.png" alt="Netlist" className="h-15 mr-5 inline"/>
          Schematic Generation →</h2>
        <hr className="border-[0.5px] border-emerald-800 mb-4 w-full"/>
        <div className="my-8 w-full">
          <div ref={graphContainerRef} className={fullscreen ? "fixed inset-0 p-5 bg-gray-200" : "relative"} style={fullscreen ? {width: "100vw", height: "100vh"} : {}}>
            <div className="flex justify-between items-center w-full mb-2">
              <h3 className="text-xl font-bold">Component Graph</h3>
              <button
                className={fullscreen ? "px-3 py-1 bg-gray-700 text-white rounded hover:bg-gray-600" : "px-3 py-1 bg-gray-400 text-white rounded hover:bg-gray-500"}
                style={{zIndex: 60}}
                onClick={() => {
                  setFullscreen(f => !f);
                }}
              >
                {fullscreen ? "Exit Fullscreen" : "Fullscreen"}
              </button>
            </div>
            <svg ref={graphRef} className={fullscreen ? "border rounded shadow w-full h-[90vh] bg-gray-800" : "border rounded shadow w-full bg-gray-800"} />
          </div>
        </div>
        <h2 className="text-2xl font-bold my-5 uppercase tracking-widest text-emerald-800 w-full">
          <img src="/board.png" alt="Layout" className="h-15 mr-5 inline"/>
          Board Layout →</h2>
        <hr className="border-[0.5px] border-emerald-800 mb-4 w-full"/>
        <h3 className="text-xl font-bold mb-2">Warning! The agent will briefly control the mouse and keyboard.</h3>
        {/* <div className="my-8 w-full">
          <h3 className="text-xl font-bold mb-2">Component Layouts</h3>
          <div className="grid grid-cols-2 gap-4 w-full">
            {Object.entries(layouts).map(([compId, layoutObj]) => {
              const layout = layoutObj as string;
              return (
                <div key={compId} className="border rounded p-4 bg-white">
                  <h4 className="font-bold mb-2">{compId}</h4>
                  <img src={layout} alt={compId} className="w-full h-auto" />
                </div>
              );
            })}
          </div>
          <h3 className="text-xl font-bold my-4">Final PCB Layout</h3>
        </div> */}
      </div>
    </main>
  );
}